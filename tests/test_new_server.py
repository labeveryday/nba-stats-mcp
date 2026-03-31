"""Tests for the JSON-first v2 server (server.py)."""

import json
from unittest.mock import patch

import pytest
from mcp.types import TextContent

from nba_mcp_server.server import (
    get_all_teams,
    get_all_time_leaders,
    get_player_info,
    mcp,
    resolve_player_id,
)


class TestNewServerV2:
    @pytest.mark.asyncio
    async def test_list_tools_parity(self):
        tools = await mcp.list_tools()
        assert len(tools) == 30
        tool_names = [t.name for t in tools]
        for expected in (
            "get_todays_scoreboard",
            "get_box_score",
            "get_player_info",
            "get_shot_chart",
            "get_team_advanced_stats",
        ):
            assert expected in tool_names

    @pytest.mark.asyncio
    async def test_get_all_teams_returns_json_and_ids(self):
        result = await get_all_teams()
        assert len(result) == 1
        assert isinstance(result[0], TextContent)
        payload = json.loads(result[0].text)

        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "get_all_teams"
        assert "NBA Teams:" in payload.get("text", "")
        assert "1610612747" in payload.get("text", "")
        assert payload["entities"]["teams"]

    @pytest.mark.asyncio
    async def test_resolve_player_id_includes_assets(self, mock_httpx_response):
        mock_data = {
            "resultSets": [
                {
                    "headers": [
                        "PERSON_ID",
                        "OTHER",
                        "DISPLAY_FIRST_LAST",
                        "X",
                        "Y",
                        "Z",
                        "A",
                        "B",
                        "C",
                        "D",
                        "E",
                        "IS_ACTIVE",
                    ],
                    "rowSet": [
                        [
                            2544,
                            None,
                            "LeBron James",
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            1,
                        ],
                        [
                            201939,
                            None,
                            "Stephen Curry",
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            1,
                        ],
                    ],
                }
            ]
        }

        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            result = await resolve_player_id(query="LeBron", limit=5)

        payload = json.loads(result[0].text)
        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "resolve_player_id"
        assert "2544" in payload.get("text", "")
        assert "1040x760/2544.png" in payload.get("text", "")
        assert "260x190/2544.png" in payload.get("text", "")
        assert any(p.get("player_id") == "2544" for p in payload["entities"]["players"])

    @pytest.mark.asyncio
    async def test_get_player_info_includes_team_and_fields(self):
        mock_data = {
            "resultSets": [
                {
                    "headers": [
                        "PERSON_ID",
                        "FIRST_NAME",
                        "LAST_NAME",
                        "DISPLAY_FIRST_LAST",
                        "BIRTHDATE",
                        "SCHOOL",
                        "COUNTRY",
                        "HEIGHT",
                        "WEIGHT",
                        "SEASON_EXP",
                        "JERSEY",
                        "POSITION",
                        "ROSTERSTATUS",
                        "TEAM_ID",
                        "TEAM_NAME",
                        "TEAM_ABBREVIATION",
                    ],
                    "rowSet": [
                        [
                            2544,
                            "LeBron",
                            "James",
                            "LeBron James",
                            "1984-12-30T00:00:00",
                            "St. Vincent-St. Mary HS (OH)",
                            "USA",
                            "6-9",
                            "250",
                            "21",
                            "23",
                            "F",
                            "Active",
                            1610612747,
                            "Los Angeles Lakers",
                            "LAL",
                        ]
                    ],
                }
            ]
        }

        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            result = await get_player_info(player_id="2544")

        payload = json.loads(result[0].text)
        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "get_player_info"
        assert "Player ID: 2544" in payload.get("text", "")
        assert "1040x760/2544.png" in payload.get("text", "")
        assert any(t.get("team_id") == "1610612747" for t in payload["entities"]["teams"])

    @pytest.mark.asyncio
    async def test_fallback_tool_is_wrapped_as_json(self, sample_all_time_leaders_data):
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = sample_all_time_leaders_data
            result = await get_all_time_leaders(stat_category="points", limit=3)

        payload = json.loads(result[0].text)
        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "get_all_time_leaders"
        assert "text" in payload and "LeBron James" in payload["text"]
        assert "entities" in payload
