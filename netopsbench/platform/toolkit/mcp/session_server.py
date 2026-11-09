"""Episode-scoped HTTP transport over the existing diagnostic tool gateway."""

import asyncio
import secrets
import socket
from contextlib import asynccontextmanager, contextmanager


@asynccontextmanager
async def session_mcp(context, *, host="127.0.0.1", max_tool_calls=None, record_trace=True):
    import uvicorn
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    from netopsbench.platform.toolkit.fastmcp_server import create_server

    token = secrets.token_urlsafe(32)
    auth = StaticTokenVerifier(tokens={token: {"client_id": "diagnosis", "scopes": []}})
    server = create_server(
        gateway=context.tools, auth=auth, trace=context.trace if record_trace else None, max_tool_calls=max_tool_calls
    )
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
    listener = socket.socket()
    listener.bind((host, 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=5)

    class EpisodeServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # The acceptance runner owns cancellation and fault recovery.
            yield

    http = EpisodeServer(config)
    task = asyncio.create_task(http.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if http.started:
                break
            if task.done():
                await task
                raise RuntimeError("Episode MCP server failed to start")
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError("Episode MCP server startup timed out")
        yield {"url": f"http://{host}:{port}/mcp", "token": token, "port": port}
    finally:
        http.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            listener.close()
