"""Typed stdio MCP surface; no arbitrary commands or adoption of existing devices."""

from typing import Any

from mcp.server import MCPServer


def make_server(queue):
    server = MCPServer(
        "simqueue",
        version="0.1.0",
        instructions=(
            "Acquire an isolated simulator lease. Poll/heartbeat queued requests. "
            "Use only its returned UUID. Share only by explicit sharing capability. "
            "Register test process PIDs before releasing. Release in finally, then verify cleanup. "
            "Keep capability tokens private; status never returns them."
        ),
    )

    @server.tool(structured_output=True)
    def inventory() -> dict[str, Any]:
        """List installed device types, runtimes and existing devices; makes no changes."""
        return queue.backend.inventory()

    @server.tool(structured_output=True)
    def acquire(
        owner: str, task: str, device_type: str, runtime: str, ttl: int = 900, share: bool = False
    ) -> dict[str, Any]:
        """Request a disposable device using exact installed identifiers. Returns a private
        lease_token once; retain it. Capacity queues requests FIFO. Explicit share=True
        also returns a private share_token for collaborators; sharing is never automatic."""
        return queue.acquire(owner, task, device_type, runtime, ttl, share)

    @server.tool(structured_output=True)
    def join(share_token: str, owner: str, task: str, ttl: int = 900) -> dict[str, Any]:
        """Join an explicitly shared live device; retain the returned private lease_token."""
        return queue.join(share_token, owner, task, ttl)

    @server.tool(structured_output=True)
    def poll(lease_token: str) -> dict[str, Any]:
        """Check allocation or cleanup and reconcile pending work without extending the TTL."""
        return queue.poll(lease_token)

    @server.tool(structured_output=True)
    def heartbeat(lease_token: str) -> dict[str, Any]:
        """Extend a queued or held lease. Heartbeat before TTL expires (default 15 minutes)."""
        return queue.poll(lease_token, renew=True)

    @server.tool(structured_output=True)
    def boot(lease_token: str) -> dict[str, Any]:
        """Boot the caller's allocated device and wait for boot completion."""
        return queue.boot(lease_token)

    @server.tool(structured_output=True)
    def track_process(lease_token: str, pid: int) -> dict[str, Any]:
        """Protect a test/build process by PID plus start identity before release.
        Cleanup waits until every registered process exits, even after every lease releases."""
        return queue.track_process(lease_token, pid)

    @server.tool(structured_output=True)
    def release(lease_token: str) -> dict[str, Any]:
        """Idempotently release/cancel a request. Only the final holder permits cleanup.
        Check device_state=deleted; attention means preservation requires follow-up."""
        return queue.release(lease_token)

    @server.tool(structured_output=True)
    def status() -> dict[str, Any]:
        """Read shared queue state, ownership and cleanup evidence without exposing tokens."""
        return queue.status()

    @server.tool(structured_output=True)
    def sweep() -> dict[str, Any]:
        """Reconcile interrupted operations, expired dead owners and cleanup; grant queued work.
        Unverifiable activity or identity is preserved for inspection, never force-deleted."""
        return queue.status(sweep=True)

    return server
