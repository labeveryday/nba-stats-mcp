"""NBA MCP Server - Access NBA statistics via Model Context Protocol."""

import asyncio

from nba_mcp_server.server import async_main

__version__ = "0.2.0"


def main():
    """Entry point that runs the async main function."""
    asyncio.run(async_main())


__all__ = ["main", "__version__"]
