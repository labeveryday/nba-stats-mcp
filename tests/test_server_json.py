"""JSON-first tests for nba_mcp_server.server."""

import json
from unittest.mock import patch

import pytest
from mcp.types import TextContent

from nba_mcp_server.server import (
    fetch_nba_data,
    find_game_id,
    get_all_teams,
    get_league_leaders,
    mcp,
    resolve_player_id,
)


def _as_json(result: list[TextContent]) -> dict:
    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    return json.loads(result[0].text)


class TestJsonServerBasics:
    def test_server_instance(self):
        assert mcp is not None
        assert mcp.name == "nba-stats-mcp"

    @pytest.mark.asyncio
    async def test_list_tools_count(self):
        tools = await mcp.list_tools()
        assert len(tools) == 30


class TestJsonEnvelope:
    @pytest.mark.asyncio
    async def test_get_all_teams_returns_json(self):
        payload = _as_json(await get_all_teams())
        assert payload["entity_type"] == "tool_result"
        assert payload["tool_name"] == "get_all_teams"
        assert "text" in payload
        assert "NBA Teams:" in payload["text"]
        assert "cdn.nba.com/logos/nba" in payload["text"]
        assert "entities" in payload
        assert any(t.get("team_id") == "1610612747" for t in payload["entities"]["teams"])

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
                    ],
                }
            ]
        }
        mock_response = mock_httpx_response(200, mock_data)

        with patch("nba_mcp_server.server.http_client") as mock_client:
            mock_client.get.return_value = mock_response
            payload = _as_json(
                await resolve_player_id(query="LeBron", limit=5)
            )

        assert payload["tool_name"] == "resolve_player_id"
        assert "2544" in payload["text"]
        assert "cdn.nba.com/headshots/nba/latest/1040x760/2544.png" in payload["text"]
        assert any(p.get("player_id") == "2544" for p in payload["entities"]["players"])

    @pytest.mark.asyncio
    async def test_find_game_id_without_date_uses_schedule(self):
        schedule_data = {
            "leagueSchedule": {
                "gameDates": [
                    {
                        "games": [
                            {
                                "gameId": "0022500123",
                                "gameDateTimeEst": "2025-10-21T00:00:00Z",
                                "gameStatus": 3,
                                "gameStatusText": "Final",
                                "homeTeam": {
                                    "teamId": 1610612747,
                                    "teamCity": "Los Angeles",
                                    "teamName": "Lakers",
                                },
                                "awayTeam": {
                                    "teamId": 1610612744,
                                    "teamCity": "Golden State",
                                    "teamName": "Warriors",
                                },
                            }
                        ]
                    }
                ]
            }
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = schedule_data
            payload = _as_json(
                await find_game_id(home_team="Lakers", away_team="Warriors")
            )

        assert "0022500123" in payload["text"]
        assert "2025-10-21" in payload["text"]

    @pytest.mark.asyncio
    async def test_get_league_leaders_returns_json(self):
        mock_data = {
            "resultSets": [
                {
                    "headers": ["PLAYER_ID", "PLAYER_NAME", "TEAM_ABBREVIATION", "PTS"],
                    "rowSet": [
                        [2544, "LeBron James", "LAL", 27.5],
                        [201939, "Stephen Curry", "GSW", 28.1],
                    ],
                }
            ]
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            payload = _as_json(
                await get_league_leaders(stat_type="Points", season="2024-25", limit=2)
            )

        assert payload["tool_name"] == "get_league_leaders"
        assert "League Leaders" in payload.get("text", "")
        assert any(p.get("player_id") in {"2544", "201939"} for p in payload["entities"]["players"])


class TestFetchNBAData:
    @pytest.mark.asyncio
    async def test_fetch_nba_data_success(self, mock_httpx_response):
        mock_data = {"test": "data"}
        mock_response = mock_httpx_response(200, mock_data)

        with patch("nba_mcp_server.server.http_client") as mock_client:
            mock_client.get.return_value = mock_response
            result = await fetch_nba_data("https://test.com")

        assert result == mock_data


class TestAllToolsImplemented:
    @pytest.mark.asyncio
    async def test_all_tools_return_valid_json(self):
        """Verify all 30 tools return valid JSON envelopes (no 'Unknown tool' text)."""
        import nba_mcp_server.server as srv

        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = None

            tools = await mcp.list_tools()
            tool_names = [t.name for t in tools]
            assert len(tool_names) == 30

            sample_args = {
                "get_server_info": {},
                "resolve_team_id": {"query": "Lakers", "limit": 3},
                "resolve_player_id": {"query": "LeBron", "limit": 3},
                "find_game_id": {"team": "Lakers", "lookback_days": 30, "limit": 3},
                "get_todays_scoreboard": {},
                "get_scoreboard_by_date": {"date": "20250101"},
                "get_game_details": {"game_id": "0022500001"},
                "get_box_score": {"game_id": "0022500001"},
                "search_players": {"query": "James"},
                "get_player_info": {"player_id": "2544"},
                "get_player_season_stats": {"player_id": "2544", "season": "2024-25"},
                "get_player_game_log": {"player_id": "2544", "season": "2024-25"},
                "get_player_career_stats": {"player_id": "2544"},
                "get_player_hustle_stats": {"player_id": "2544", "season": "2024-25"},
                "get_league_hustle_leaders": {"stat_category": "deflections", "season": "2024-25"},
                "get_player_defense_stats": {"player_id": "2544", "season": "2024-25"},
                "get_all_time_leaders": {"stat_category": "points", "limit": 3},
                "get_all_teams": {},
                "get_team_roster": {"team_id": "1610612747", "season": "2024-25"},
                "get_standings": {"season": "2024-25"},
                "get_league_leaders": {"stat_type": "Points", "season": "2024-25", "limit": 5},
                "get_schedule": {"team_id": "1610612747", "days_ahead": 7},
                "get_player_awards": {"player_id": "2544"},
                "get_season_awards": {"season": "2015-16"},
                "get_shot_chart": {"player_id": "2544", "season": "2024-25"},
                "get_shooting_splits": {"player_id": "2544", "season": "2024-25"},
                "get_play_by_play": {"game_id": "0022500001", "start_period": 1, "end_period": 1},
                "get_game_rotation": {"game_id": "0022500001"},
                "get_player_advanced_stats": {"player_id": "2544", "season": "2024-25"},
                "get_team_advanced_stats": {"team_id": "1610612747", "season": "2024-25"},
            }

            for name in tool_names:
                args = sample_args.get(name, {})
                fn = getattr(srv, name)
                payload = _as_json(await fn(**args))
                assert payload["tool_name"] == name
