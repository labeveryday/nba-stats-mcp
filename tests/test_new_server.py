"""Tests for the JSON-first v3 server (server.py) — structured data + name resolution."""

import json
from unittest.mock import patch

import pytest
from mcp.types import TextContent

from nba_mcp_server.server import (
    get_player_info,
    get_leaders,
    get_shooting_data,
    mcp,
    resolve_player_id,
)


class TestNewServerV3:
    @pytest.mark.asyncio
    async def test_list_tools_parity(self):
        tools = await mcp.list_tools()
        assert len(tools) == 21
        tool_names = [t.name for t in tools]
        for expected in (
            "get_scoreboard",
            "get_box_score",
            "get_player_info",
            "get_shooting_data",
            "get_team_advanced_stats",
            "compare_players",
            "daily_summary",
            "team_overview",
        ):
            assert expected in tool_names

    @pytest.mark.asyncio
    async def test_resolve_player_id_returns_data_field(self):
        mock_data = {
            "resultSets": [{
                "headers": ["PERSON_ID", "OTHER", "DISPLAY_FIRST_LAST", "X", "Y", "Z", "A", "B", "C", "D", "E", "IS_ACTIVE"],
                "rowSet": [
                    [2544, None, "LeBron James", None, None, None, None, None, None, None, None, 1],
                    [201939, None, "Stephen Curry", None, None, None, None, None, None, None, None, 1],
                ],
            }]
        }

        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            result = await resolve_player_id(query="LeBron", limit=5)

        payload = json.loads(result[0].text)
        assert payload["entity_type"] == "tool_result"
        assert payload["schema_version"] == "3.0"
        assert payload["tool_name"] == "resolve_player_id"
        assert "data" in payload
        assert payload["data"]["matches"][0]["player_id"] == 2544
        assert payload["data"]["matches"][0]["name"] == "LeBron James"

    @pytest.mark.asyncio
    async def test_get_player_info_accepts_name(self):
        player_lookup_data = {
            "resultSets": [{
                "headers": ["PERSON_ID", "OTHER", "DISPLAY_FIRST_LAST", "X", "Y", "Z", "A", "B", "C", "D", "E", "IS_ACTIVE"],
                "rowSet": [[2544, None, "LeBron James", None, None, None, None, None, None, None, None, 1]],
            }]
        }
        player_info_data = {
            "resultSets": [{
                "headers": ["PERSON_ID", "FIRST_NAME", "LAST_NAME", "DISPLAY_FIRST_LAST",
                            "BIRTHDATE", "SCHOOL", "COUNTRY", "HEIGHT", "WEIGHT",
                            "SEASON_EXP", "JERSEY", "POSITION", "ROSTERSTATUS",
                            "TEAM_ID", "TEAM_NAME", "TEAM_ABBREVIATION"],
                "rowSet": [[2544, "LeBron", "James", "LeBron James",
                            "1984-12-30", "St. Vincent", "USA", "6-9", "250",
                            "21", "23", "F", "Active",
                            1610612747, "Los Angeles Lakers", "LAL"]],
            }]
        }

        call_count = [0]

        async def mock_fetch(url, params=None):
            call_count[0] += 1
            if "commonallplayers" in url:
                return player_lookup_data
            return player_info_data

        with patch("nba_mcp_server.server.fetch_nba_data", side_effect=mock_fetch):
            result = await get_player_info(player="LeBron James")

        payload = json.loads(result[0].text)
        assert payload["tool_name"] == "get_player_info"
        assert "data" in payload
        assert payload["data"]["name"] == "LeBron James"
        assert payload["data"]["player_id"] == 2544
        # Text should be clean — no CDN URLs or Player IDs
        assert "cdn.nba.com" not in payload.get("text", "")

    @pytest.mark.asyncio
    async def test_get_leaders_all_time(self, sample_all_time_leaders_data):
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = sample_all_time_leaders_data
            result = await get_leaders(category="points", scope="all_time", limit=3)

        payload = json.loads(result[0].text)
        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "get_leaders"
        assert "data" in payload
        assert payload["data"]["scope"] == "all_time"
        assert payload["data"]["leaders"][0]["player_name"] == "LeBron James"

    @pytest.mark.asyncio
    async def test_get_shooting_data_splits(self, sample_shooting_splits_data):
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = sample_shooting_splits_data
            result = await get_shooting_data(player="2544", data_type="splits", season="2024-25")

        payload = json.loads(result[0].text)
        assert payload["tool_name"] == "get_shooting_data"
        assert "data" in payload
        assert payload["data"]["data_type"] == "splits"
        assert payload["data"]["fg_pct"] == 0.475

    @pytest.mark.asyncio
    async def test_get_shooting_data_chart(self, sample_shot_chart_data):
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = sample_shot_chart_data
            result = await get_shooting_data(player="2544", data_type="chart", season="2024-25")

        payload = json.loads(result[0].text)
        assert payload["tool_name"] == "get_shooting_data"
        assert "data" in payload
        assert payload["data"]["data_type"] == "chart"
        assert payload["data"]["total_shots"] == 5
        assert payload["data"]["made"] == 3
