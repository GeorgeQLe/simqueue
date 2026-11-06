"""Typed stdio MCP surface; no arbitrary commands or adoption of existing devices."""

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .backend import Attention


def invoke(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (Attention, ValueError) as exc:
        raise ToolError(str(exc)) from None


def make_server(queue):
    server = MCPServer(
        "simqueue",
        version="0.2.2",
        instructions=(
            "Acquire an isolated simulator lease. Poll/heartbeat queued requests. "
            "Use its returned device_set with simctl --set and only its returned UUID. Share only by explicit sharing capability. "
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
        owner: str, task: str, device_type: str, runtime: str, ttl: int = 900, share: bool = False
    ) -> dict[str, Any]:
        """Request a disposable device using exact installed identifiers. Returns a private
        lease_token once; retain it. Capacity queues requests FIFO. Explicit share=True
        also returns a private share_token for collaborators; sharing is never automatic."""
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
        """Boot the caller's allocated device and wait for boot completion."""
        return invoke(queue.boot, lease_token)

    @server.tool(structured_output=True)
    def track_process(lease_token: str, pid: int) -> dict[str, Any]:
        """Protect a test/build process by PID plus start identity before release.
        Cleanup waits until every registered process exits, even after every lease releases."""
        return invoke(queue.track_process, lease_token, pid)

    @server.tool(structured_output=True)
    def release(lease_token: str) -> dict[str, Any]:
        """Idempotently release/cancel a request. Only the final holder permits cleanup.
        Check device_state=deleted; attention means preservation requires follow-up."""
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
    def status() -> dict[str, Any]:
        """Read shared queue state, ownership and cleanup evidence without exposing tokens."""
        return invoke(queue.status)

    @server.tool(structured_output=True)
    def sweep() -> dict[str, Any]:
        """Reconcile interrupted operations, expired dead owners and cleanup; grant queued work.
        Unverifiable activity or identity is preserved for inspection, never force-deleted."""
        return invoke(queue.status, sweep=True)

    return server
