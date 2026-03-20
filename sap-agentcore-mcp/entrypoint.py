"""
Entrypoint wrapper that captures Authorization header and passes it to MCP tools.
This wraps FastMCP's ASGI app with a lightweight middleware.
"""
import logging
import uvicorn
from sap_odata_mcp_server import mcp, auth_token_var

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("entrypoint")


class AuthHeaderMiddleware:
    """ASGI middleware to capture Authorization header into context var."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key == b"authorization":
                    token_val = value.decode()
                    if token_val.lower().startswith("bearer "):
                        auth_token_var.set(token_val[7:])
                        logger.info(f"Captured auth token (len={len(token_val)-7})")
                    break
        await self.app(scope, receive, send)


if __name__ == "__main__":
    logger.info("Starting MCP Server with auth header capture...")
    # Get FastMCP's ASGI app and wrap it
    app = mcp.streamable_http_app()
    wrapped = AuthHeaderMiddleware(app)
    uvicorn.run(wrapped, host="0.0.0.0", port=8000)
