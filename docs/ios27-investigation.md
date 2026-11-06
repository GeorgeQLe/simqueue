# iOS 27 simulator installation investigation

Observed 6 November 2026 on Apple silicon, macOS 27.2 (26B123), stable Xcode 27.1
(27A9275), CoreSimulator 1174.9.2 and iOS 27.0 runtime 24A5390f. These are local
host observations, not a claim about all iOS 27 installations.

## Findings

- The earlier external-image test exceeded its 120-second `simctl install` bound.
  Its device-local MobileInstallation log nevertheless recorded successful
  placeholder installation (63.64 seconds) followed by successful developer
  installation (20.95 seconds). Those phase durations are not the total client
  wall time and must not be added to infer when the client should have returned.
- A fresh internal-device control timed out after 45.25 seconds. Its install log
  did not record the target app request. The timeout is not confined to the T9
  image or to simqueue's image staging code.
- An isolated minimal-app probe reached successful developer installation. Its
  diagnostic sampling exceeded a separate short bound, so that probe is not a
  clean end-to-end pass. Its registered installer finished and normal lease
  release subsequently verified deletion.
- An extended internal control of the original app returned native success in
  14.81 seconds. An extended external-image control returned native success in
  54.37 seconds. Both disposable devices were verified deleted.
- A pending external `simctl` sample showed
  `SimDevice installApplication:withOptions:installationInfo:error:` waiting in
  `host_support_mig_install_app_with_info`. The device logs show expensive
  first-install preflight work and later LaunchServices registration. They do not
  establish a permanent deadlock or an exact root cause.
- The first external follow-up screenshot showed the home screen after launch.
  Native install success and a launch PID alone do not prove the app rendered.
- The final 0.2.2 acceptance through two independent MCP clients passed: Contacts,
  app install, a live app process, screenshot export, shared-holder protection,
  final deletion, unchanged regular devices and normal image eject. Manual image
  review confirmed the actual game menu, unlike the first follow-up capture.
  The acceptance app came from the regular iOS 27 device; the timed internal and
  external controls above used the same app copy from the regular iOS 26.5 device.
- No Contacts migration-helper crash was found for the final acceptance device.
  Separate `AppIntentsLiveEntityService` XPC entitlement assertion crashes occurred
  in both the extended external run and final acceptance. They did not prevent
  the final game menu from rendering. No workaround for that Apple helper was
  attempted, and the longer install timeout does not claim to fix it.

## Mitigation in 0.2.2

Allow native installation up to 300 seconds, matching the existing image-device
boot budget. Require an actual successful native command exit. Preserve uncertain
staging data and identify the operation and elapsed limit in timeout errors. Keep
MCP clients configured for at least 360 seconds per tool call.

This is a bounded mitigation for intermittent startup/completion delays, not a
proven correction inside Apple's simulator. Do not retry installs blindly, infer
success from a copied bundle, restart shared CoreSimulator services automatically,
or delete a device held by another task. The original Apple-side stall remains an
open issue until a reproducible cause and stronger fix are established.

## Retained evidence and reproduction

Machine-specific reports, native stderr, stack samples, screenshots and private
recovery capabilities stay outside this repository on T9. No device data or
capability tokens belong in a public report. For repeat acceptance, run
`scripts/image_acceptance.py --output <unique-T9-directory> --runtime
com.apple.CoreSimulator.SimRuntime.iOS-27-0` with the project's Python environment.
The command is one line; it requires the host-specific helpers documented in the
README and an existing built simulator app used by this host's acceptance script.

Inventory first, preserve all regular devices, acquire through MCP, register
asynchronous processes and release in `finally`. Verify both UUID and directory
absence. If other tasks hold image devices, leave the image attached for them.
