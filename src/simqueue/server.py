"""Typed stdio MCP surface; no arbitrary commands or adoption of existing devices."""

from typing import Annotated, Any

from anyio import to_thread
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .backend import Attention
from .core import ROUTINE_RUNTIME
from .xctest import MCP_WAIT_SECONDS, XCTestRuns


def invoke(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (Attention, OSError, ValueError) as exc:
        raise ToolError(str(exc)) from None


def make_server(queue):
    runs = XCTestRuns(queue)
    server = MCPServer(
        "simqueue",
        version="0.2.2",
        instructions=(
            "For built XCTest plans, submit run_xctest and use xctest_wait with its run ID. "
            "For multi-minute waits, use the local CLI xctest-wait for one longer wait. "
            "The worker manages queueing, boot, test execution and cleanup even after the client disconnects. "
            "Use iOS 26.5 for routine testing when available. For manual operations, acquire one isolated simulator lease and poll/heartbeat queued requests. "
            "Use its returned device_set with simctl --set and only its returned UUID. Share only by explicit sharing capability. "
            "For Xcode 27 tests, append returned xcodebuild_args as individual arguments; the device-set environment variable alone is insufficient. "
            "Register test process PIDs before releasing. Release in finally, then verify cleanup. "
            "Allocation ready does not mean boot ready. Call boot and require success before app work. "
            "Keep capability tokens private; status never returns them."
        ),
    )

    @server.tool(structured_output=True)
    def inventory() -> dict[str, Any]:
        """List installed device types, runtimes and existing devices; attaches configured image if needed."""
        return invoke(queue.backend.inventory)

    @server.tool(structured_output=True)
    def acquire(
        owner: str,
        task: str,
        device_type: str,
        runtime: str = ROUTINE_RUNTIME,
        ttl: int = 900,
        share: bool = False,
    ) -> dict[str, Any]:
        """Request a disposable device using exact installed identifiers. Returns a private
        lease_token once; retain it. Requests queue FIFO for capacity to create a fresh
        device. Use share=True to get a private share_token for a same-device handoff.
        The incoming agent must join before the current holder releases. Omit runtime for iOS 26.5
        when it is installed; otherwise select an exact installed identifier."""
        return invoke(queue.acquire, owner, task, device_type, runtime, ttl, share)

    @server.tool(structured_output=True)
    def join(share_token: str, owner: str, task: str, ttl: int = 900) -> dict[str, Any]:
        """Join an explicitly shared live device; retain the returned private lease_token."""
        return invoke(queue.join, share_token, owner, task, ttl)

    @server.tool(structured_output=True)
    def poll(lease_token: str) -> dict[str, Any]:
        """Check allocation or cleanup and reconcile pending work without extending the TTL."""
        return invoke(queue.poll, lease_token)

    @server.tool(structured_output=True)
    def heartbeat(lease_token: str) -> dict[str, Any]:
        """Extend a queued or held lease. Heartbeat before TTL expires (default 15 minutes)."""
        return invoke(queue.poll, lease_token, renew=True)

    @server.tool(structured_output=True)
    def boot(lease_token: str) -> dict[str, Any]:
        """Boot the caller's device; require boot_ready before tests.
        Simqueue checks the same UUID once after a native timeout. Status and
        heartbeat report the running operation. If readiness is still uncertain
        or migration failed, inspect diagnostics and release the lease.
        """
        return invoke(queue.boot, lease_token)

    @server.tool(structured_output=True)
    def track_process(lease_token: str, pid: int) -> dict[str, Any]:
        """Protect a test/build process by PID plus start identity before release.
        Cleanup waits until every registered process exits, even after every lease releases."""
        return invoke(queue.track_process, lease_token, pid)

    @server.tool(structured_output=True)
    def release(lease_token: str) -> dict[str, Any]:
        """Idempotently release/cancel a request. Only the final holder permits cleanup.
        The final holder checks device_state=deleted. If another lease still holds
        the device, it remains available. Attention means follow-up is required."""
        return invoke(queue.release, lease_token)

    @server.tool(structured_output=True)
    def install(lease_token: str, app_path: str) -> dict[str, Any]:
        """Stage a built simulator .app inside the external storage image, install it on the booted
        leased UUID, then remove the staging copy. Returns its bundle identifier."""
        return invoke(queue.install, lease_token, app_path)

    @server.tool(structured_output=True)
    def capture(lease_token: str, filename: str = "screenshot.png") -> dict[str, Any]:
        """Capture a booted leased device inside the image, then export a validated PNG
        to external storage. Use this instead of asking Apple's screenshot service to write directly
        to a removable volume. Returns a retained path, dimensions and SHA-256."""
        return invoke(queue.capture, lease_token, filename)

    @server.tool(structured_output=True)
    def run_xctest(
        owner: str,
        task: str,
        device_type: str,
        arguments: list[str],
        runtime: str = ROUTINE_RUNTIME,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a built .xctestrun plan. Pass absolute -xctestrun and -resultBundlePath,
        with optional -only-testing selectors. A detached worker owns the lease.
        Supply a client-generated 32-character lowercase hex run ID to safely retry
        the same submission after losing its response."""
        return invoke(runs.submit, owner, task, device_type, arguments, runtime, run_id)

    @server.tool(structured_output=True)
    def xctest_status(run_id: str) -> dict[str, Any]:
        """Read progress, last native boot phase, test outcome and cleanup for one run."""
        return invoke(runs.status, run_id)

    @server.tool(structured_output=True)
    async def xctest_wait(
        run_id: str,
        timeout_seconds: Annotated[int, Field(ge=1, le=MCP_WAIT_SECONDS)] = MCP_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Wait in the tool for one managed run's outcome and cleanup or attention.
        A timeout returns only the run ID and phase. The detached worker keeps running.
        Use xctest_status only when intermediate progress or diagnostics are needed.
        """
        return await to_thread.run_sync(invoke, runs.wait, run_id, timeout_seconds)

    @server.tool(structured_output=True)
    def status(history: bool = False) -> dict[str, Any]:
        """Read active queue state and attention. Set history for the full retained journal view."""
        return invoke(queue.status, compact=not history)

    @server.tool(structured_output=True)
    def sweep() -> dict[str, Any]:
        """Reconcile interrupted operations, expired dead owners and cleanup; grant queued work.
        Unverifiable activity or identity is preserved for inspection, never force-deleted."""
        return invoke(queue.status, sweep=True)

    return server
