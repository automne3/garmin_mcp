"""
SSE/HTTP Server for Garmin MCP

This module provides HTTP endpoints with Server-Sent Events (SSE) transport
for compatibility with ChatGPT and other HTTP-based MCP clients.

Usage:
    garmin-mcp-sse                    # Start SSE server on default port 8000
    garmin-mcp-sse --port 3000        # Start on custom port
    garmin-mcp-sse --host 0.0.0.0     # Bind to all interfaces
"""

import argparse
import logging
import os
import sys

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse
import uvicorn

from mcp.server.sse import SseServerTransport

from garmin_mcp.oauth_google import GoogleOAuthValidator, extract_bearer_token

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_sse_app():
    """Create and configure the SSE MCP server application."""
    from garmin_mcp import (
        init_api,
        email,
        password,
        activity_management,
        health_wellness,
        user_profile,
        devices,
        gear_management,
        weight_management,
        challenges,
        training,
        workouts,
        workout_templates,
        data_management,
        womens_health,
        memory_context,
    )
    from mcp.server.fastmcp import FastMCP

    # Initialize Garmin client
    garmin_client = init_api(email, password)
    if not garmin_client:
        logger.error("Failed to initialize Garmin Connect client.")
        raise RuntimeError("Failed to initialize Garmin Connect client")

    logger.info("Garmin Connect client initialized successfully.")

    # Configure all modules with the Garmin client
    activity_management.configure(garmin_client)
    health_wellness.configure(garmin_client)
    user_profile.configure(garmin_client)
    devices.configure(garmin_client)
    gear_management.configure(garmin_client)
    weight_management.configure(garmin_client)
    challenges.configure(garmin_client)
    training.configure(garmin_client)
    workouts.configure(garmin_client)
    data_management.configure(garmin_client)
    womens_health.configure(garmin_client)

    # Create the MCP app
    mcp_app = FastMCP("Garmin Connect v1.0")

    # Register tools from all modules
    mcp_app = activity_management.register_tools(mcp_app)
    mcp_app = health_wellness.register_tools(mcp_app)
    mcp_app = user_profile.register_tools(mcp_app)
    mcp_app = devices.register_tools(mcp_app)
    mcp_app = gear_management.register_tools(mcp_app)
    mcp_app = weight_management.register_tools(mcp_app)
    mcp_app = challenges.register_tools(mcp_app)
    mcp_app = training.register_tools(mcp_app)
    mcp_app = workouts.register_tools(mcp_app)
    mcp_app = data_management.register_tools(mcp_app)
    mcp_app = womens_health.register_tools(mcp_app)
    mcp_app = memory_context.register_tools(mcp_app)

    # Register resources (workout templates)
    mcp_app = workout_templates.register_resources(mcp_app)

    return mcp_app


# Global MCP app instance (lazy initialization)
_mcp_app = None


def get_mcp_app():
    """Get or create the MCP app instance."""
    global _mcp_app
    if _mcp_app is None:
        _mcp_app = create_sse_app()
    return _mcp_app


# Create SSE transport
sse_transport = SseServerTransport("/messages/")


class OAuthMiddleware(BaseHTTPMiddleware):
    """Require OAuth Bearer token for protected endpoints."""

    def __init__(self, app, validator: GoogleOAuthValidator, protected_prefixes: tuple[str, ...]):
        super().__init__(app)
        self._validator = validator
        self._protected_prefixes = protected_prefixes

    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if path.startswith("/.well-known") or path.startswith("/sse/.well-known"):
            return await call_next(request)

        if path.startswith(self._protected_prefixes):
            token = extract_bearer_token(request.headers.get("authorization"))
            ok, payload, error = self._validator.validate_token(token)
            if not ok:
                return JSONResponse(
                    {"error": "unauthorized", "message": error},
                    status_code=401,
                )
            request.state.oauth = payload

        return await call_next(request)


async def handle_sse(request):
    """Handle SSE connection requests at /sse endpoint."""
    mcp_app = get_mcp_app()
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_app._mcp_server.run(
            streams[0], streams[1], mcp_app._mcp_server.create_initialization_options()
        )


async def handle_messages(request):
    """Handle POST messages from SSE clients."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


async def health_check(request):
    """Health check endpoint."""
    return JSONResponse({"status": "ok", "service": "garmin-mcp-sse"})


async def info(request):
    """Server info endpoint."""
    return JSONResponse({
        "name": "Garmin Connect MCP Server",
        "version": "1.0.0",
        "transport": "sse",
        "endpoints": {
            "sse": "/sse",
            "messages": "/messages/",
            "health": "/health",
        }
    })


def _origin_from_request(request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _oauth_authorization_server_metadata() -> dict:
    return {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "scopes_supported": ["openid", "email", "profile"],
    }


async def well_known(request):
    """Serve OAuth/OIDC discovery metadata for ChatGPT connector."""
    doc_name = request.path_params.get("doc_name", "")
    origin = _origin_from_request(request)
    resource = f"{origin}/sse"

    if doc_name in ("oauth-authorization-server", "openid-configuration"):
        return JSONResponse(_oauth_authorization_server_metadata())

    if doc_name == "oauth-protected-resource":
        return JSONResponse(
            {
                "resource": resource,
                "authorization_servers": ["https://accounts.google.com"],
                "scopes_supported": ["openid", "email", "profile"],
            }
        )

    return JSONResponse({"error": "not_found"}, status_code=404)


def create_starlette_app():
    """Create the Starlette ASGI application with SSE routes."""
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    cache_ttl = int(os.getenv("OAUTH_TOKENINFO_CACHE_SECONDS", "600"))
    validator = GoogleOAuthValidator(client_id, cache_ttl_seconds=cache_ttl)

    # CORS middleware for cross-origin requests
    middleware = [
        Middleware(
            OAuthMiddleware,
            validator=validator,
            protected_prefixes=("/sse", "/messages", "/messages/"),
        ),
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],  # Configure appropriately for production
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]

    app = Starlette(
        debug=os.getenv("DEBUG", "false").lower() == "true",
        routes=[
            Route("/health", health_check, methods=["GET"]),
            Route("/", info, methods=["GET"]),
            Route("/.well-known/{doc_name}", well_known, methods=["GET"]),
            Route("/.well-known/{doc_name}/{suffix}", well_known, methods=["GET"]),
            Route("/sse/.well-known/{doc_name}", well_known, methods=["GET"]),
            Route("/sse", handle_sse, methods=["GET"]),
            Route("/messages/", handle_messages, methods=["POST"]),
        ],
        middleware=middleware,
    )

    return app


def main():
    """Entry point for the SSE server."""
    parser = argparse.ArgumentParser(
        description="Garmin MCP Server with SSE transport for ChatGPT compatibility"
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "127.0.0.1"),
        help="Host to bind to (default: 127.0.0.1, use 0.0.0.0 for all interfaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="Port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )

    args = parser.parse_args()

    logger.info(f"Starting Garmin MCP SSE server on http://{args.host}:{args.port}")
    logger.info("Endpoints:")
    logger.info(f"  - SSE:      http://{args.host}:{args.port}/sse")
    logger.info(f"  - Messages: http://{args.host}:{args.port}/messages/")
    logger.info(f"  - Health:   http://{args.host}:{args.port}/health")

    app = create_starlette_app()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
