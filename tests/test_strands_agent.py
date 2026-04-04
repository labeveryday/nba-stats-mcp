"""Optional smoke test for Strands + MCP integration.

This test is skipped unless `strands-agents` is installed in the environment.
It avoids NBA API calls by only listing tools / calling get_standings.
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
async def test_strands_mcp_client_can_list_tools():
    # Strands' MCP adapter expects certain `mcp` client APIs.
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
        tools = getattr(tools_resp, "tools", tools_resp)

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

        assert "get_scoreboard" in tool_names
        assert "resolve_player_id" in tool_names
        assert "compare_players" in tool_names

        result = mcp_client.call_tool_sync(
            tool_use_id="test-season-awards",
            name="get_season_awards",
            arguments={"season": "2023-24"},
        )
        text = ""
        try:
            text = result["content"][0]["text"]
        except Exception:
            text = str(result)

        payload = json.loads(text)
        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "get_season_awards"
        assert "Joel Embiid" in payload.get("text", "")
