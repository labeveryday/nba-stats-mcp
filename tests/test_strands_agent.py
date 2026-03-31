"""Optional smoke test for Strands + MCP integration.

This test is skipped unless `strands-agents` is installed in the environment.
It avoids NBA API calls by only listing tools / calling get_server_info.
"""

import sys

import pytest

pytest.importorskip("strands")
pytest.importorskip("strands.tools.mcp")

import json

from mcp import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession
from strands.tools.mcp import MCPClient


@pytest.mark.asyncio
async def test_strands_mcp_client_can_list_tools_and_call_server_info():
    # Strands' MCP adapter expects certain `mcp` client APIs.
    # If the local `mcp` package version doesn't match what Strands expects, skip this optional test
    # rather than failing the whole suite.
    if not hasattr(ClientSession, "get_server_capabilities"):
        pytest.skip("Strands/MCP version mismatch (ClientSession.get_server_capabilities missing)")

    mcp_client = MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command=sys.executable,
                args=["-u", "-m", "nba_mcp_server"],
            )
        )
    )

    with mcp_client:
        tools_resp = mcp_client.list_tools_sync()
        # Depending on strands/mcp versions, list_tools_sync may return either:
        # - a list[Tool]
        # - a response object with `.tools`
        tools = getattr(tools_resp, "tools", tools_resp)

        # Tool objects may be pydantic-like; accept multiple shapes across strands versions.
        tool_names = []
        for t in tools:
            name = (
                getattr(t, "name", None)
                or getattr(t, "tool_name", None)
                or (t.get("name") if isinstance(t, dict) else None)
                or (t.get("tool_name") if isinstance(t, dict) else None)
            )
            if name:
                tool_names.append(name)

        assert "get_server_info" in tool_names
        assert "resolve_player_id" in tool_names

        result = mcp_client.call_tool_sync(
            tool_use_id="test-server-info",
            name="get_server_info",
            arguments={},
        )
        # result is the MCP response payload (dict with content)
        text = ""
        try:
            text = result["content"][0]["text"]
        except Exception:
            text = str(result)

        # JSON-first: tool output is a JSON envelope containing legacy text in payload["text"].
        payload = json.loads(text)
        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "get_server_info"
        assert "NBA MCP Server Info" in payload.get("text", "")
