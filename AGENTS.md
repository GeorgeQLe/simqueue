# simqueue

Keep the coordinator engine independent of MCP and simctl so concurrency and
failure recovery can be tested without booting simulators. Use the official MCP
SDK for protocol handling. Run pytest and ruff before committing.

Never adopt or delete an existing working simulator. Only recorded create intents
with an exact generated name, UUID, runtime and device type confer ownership.
Unknown activity, failed process probes, uncertain simctl outcomes and corrupt
state must preserve devices and report attention. Never uninstall shared runtimes.
No arbitrary command execution tool or HTTP listener. Tokens are local bearer
capabilities: redact them from status, diagnostics, logs and committed fixtures.

All test scratch, dependencies and build outputs live on the verified external developer volume. Real acceptance uses
separate state, records every created UUID and removes only those devices in a
finally block. Never test deletion against a user's existing devices.
