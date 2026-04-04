"""JSON-first tests for nba_mcp_server.server v0.3.0."""

import json
from unittest.mock import patch

import pytest
from mcp.types import TextContent

from nba_mcp_server.server import (
    _clean_text,
    _resolve_player,
    _resolve_team,
    fetch_nba_data,
    find_game,
    get_leaders,
    get_player_stats,
    get_scoreboard,
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
        assert len(tools) == 21


class TestNameResolution:
    @pytest.mark.asyncio
    async def test_resolve_player_numeric_fast_path(self):
        pid, name = await _resolve_player("2544")
        assert pid == 2544
        assert name == "2544"

    @pytest.mark.asyncio
    async def test_resolve_player_by_name(self):
        mock_data = {
            "resultSets": [{
                "headers": ["PERSON_ID", "OTHER", "DISPLAY_FIRST_LAST", "X", "Y", "Z", "A", "B", "C", "D", "E", "IS_ACTIVE"],
                "rowSet": [[2544, None, "LeBron James", None, None, None, None, None, None, None, None, 1]],
            }]
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            pid, name = await _resolve_player("LeBron")
        assert pid == 2544
        assert name == "LeBron James"

    @pytest.mark.asyncio
    async def test_resolve_player_no_match(self):
        mock_data = {
            "resultSets": [{
                "headers": ["PERSON_ID", "OTHER", "DISPLAY_FIRST_LAST", "X", "Y", "Z", "A", "B", "C", "D", "E", "IS_ACTIVE"],
                "rowSet": [[2544, None, "LeBron James", None, None, None, None, None, None, None, None, 1]],
            }]
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            with pytest.raises(ValueError, match="No player matched"):
                await _resolve_player("xyznonexistent")

    def test_resolve_team_numeric(self):
        tid, name = _resolve_team("1610612747")
        assert tid == 1610612747
        assert name == "Los Angeles Lakers"

    def test_resolve_team_by_name(self):
        tid, name = _resolve_team("Lakers")
        assert tid == 1610612747
        assert "Lakers" in name

    def test_resolve_team_no_match(self):
        with pytest.raises(ValueError, match="No team matched"):
            _resolve_team("zq")

    def test_resolve_team_empty(self):
        with pytest.raises(ValueError, match="Please provide"):
            _resolve_team("")


class TestTextCleaning:
    def test_strips_player_id(self):
        text = "Player stats | Player ID: 2544"
        assert "2544" not in _clean_text(text)

    def test_strips_headshot_url(self):
        text = "Info\nHeadshot: https://cdn.nba.com/headshots/nba/latest/1040x760/2544.png\nDone"
        cleaned = _clean_text(text)
        assert "cdn.nba.com" not in cleaned
        assert "Info" in cleaned
        assert "Done" in cleaned

    def test_strips_logo_url(self):
        text = "Team data | Logo: https://cdn.nba.com/logos/nba/123/global/L/logo.svg"
        assert "cdn.nba.com" not in _clean_text(text)

    def test_strips_team_id(self):
        text = "Team ID: 1610612747 | Boston"
        cleaned = _clean_text(text)
        assert "1610612747" not in cleaned
        assert "Boston" in cleaned

    def test_preserves_normal_content(self):
        text = "LeBron James scored 30 points"
        assert _clean_text(text) == text


class TestJsonEnvelope:
    @pytest.mark.asyncio
    async def test_resolve_player_id_returns_structured_data(self, mock_httpx_response):
        mock_data = {
            "resultSets": [{
                "headers": ["PERSON_ID", "OTHER", "DISPLAY_FIRST_LAST", "X", "Y", "Z", "A", "B", "C", "D", "E", "IS_ACTIVE"],
                "rowSet": [[2544, None, "LeBron James", None, None, None, None, None, None, None, None, 1]],
            }]
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            payload = _as_json(await resolve_player_id(query="LeBron", limit=5))

        assert payload["tool_name"] == "resolve_player_id"
        assert payload["schema_version"] == "3.0"
        assert "data" in payload
        assert payload["data"]["matches"][0]["player_id"] == 2544

    @pytest.mark.asyncio
    async def test_find_game_without_date_uses_schedule(self):
        schedule_data = {
            "leagueSchedule": {
                "gameDates": [{
                    "games": [{
                        "gameId": "0022500123",
                        "gameDateTimeEst": "2025-10-21T00:00:00Z",
                        "gameStatus": 3,
                        "gameStatusText": "Final",
                        "homeTeam": {"teamId": 1610612747, "teamCity": "Los Angeles", "teamName": "Lakers"},
                        "awayTeam": {"teamId": 1610612744, "teamCity": "Golden State", "teamName": "Warriors"},
                    }]
                }]
            }
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = schedule_data
            payload = _as_json(await find_game(team1="Lakers", team2="Warriors"))

        assert payload["data"]["games"][0]["game_id"] == "0022500123"

    @pytest.mark.asyncio
    async def test_get_leaders_returns_structured(self):
        mock_data = {
            "resultSets": [{
                "headers": ["PLAYER_ID", "PLAYER_NAME", "TEAM_ABBREVIATION", "PTS"],
                "rowSet": [
                    [201939, "Stephen Curry", "GSW", 28.1],
                    [2544, "LeBron James", "LAL", 27.5],
                ],
            }]
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            payload = _as_json(await get_leaders(category="points", scope="current_season", season="2024-25", limit=2))

        assert payload["tool_name"] == "get_leaders"
        assert "data" in payload
        assert payload["data"]["leaders"][0]["player_name"] == "Stephen Curry"

    @pytest.mark.asyncio
    async def test_get_scoreboard_returns_structured(self):
        mock_data = {
            "scoreboard": {
                "gameDate": "2025-01-01",
                "games": [{
                    "gameId": "0022500001",
                    "gameStatusText": "Final",
                    "period": 4,
                    "homeTeam": {"teamName": "Lakers", "teamId": 1610612747, "score": 110},
                    "awayTeam": {"teamName": "Warriors", "teamId": 1610612744, "score": 105},
                }],
            }
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            payload = _as_json(await get_scoreboard(date="20250101"))

        assert payload["tool_name"] == "get_scoreboard"
        assert "data" in payload
        assert len(payload["data"]["games"]) == 1

    @pytest.mark.asyncio
    async def test_get_player_stats_returns_structured(self):
        mock_data = {
            "resultSets": [{
                "headers": ["PLAYER_ID", "SEASON_ID", "LEAGUE_ID", "TEAM_ID", "TEAM_ABBREVIATION", "PLAYER_AGE",
                            "GP", "GS", "MIN", "FG_PCT", "X1", "X2", "FG3_PCT", "X3", "X4", "FT_PCT",
                            "X5", "X6", "REB", "AST", "X7", "STL", "BLK", "X8", "X9", "X10", "PTS"],
                "rowSet": [
                    [2544, "2024-25", "00", 1610612747, "LAL", 40,
                     65, 65, 34.2, 0.512, 0, 0, 0.351, 0, 0, 0.735,
                     0, 0, 7.1, 7.8, 0, 1.2, 0.6, 0, 0, 0, 25.3],
                ],
            }]
        }
        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = mock_data
            payload = _as_json(await get_player_stats(player="2544", stat_type="season", season="2024-25"))

        assert payload["tool_name"] == "get_player_stats"
        assert "data" in payload
        assert payload["data"]["pts"] == 25.3


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
        """Verify all 21 tools return valid JSON envelopes."""
        import nba_mcp_server.server as srv

        with patch("nba_mcp_server.server.fetch_nba_data") as mock_fetch:
            mock_fetch.return_value = None

            tools = await mcp.list_tools()
            tool_names = [t.name for t in tools]
            assert len(tool_names) == 21

            sample_args = {
                "resolve_team_id": {"query": "Lakers", "limit": 3},
                "resolve_player_id": {"query": "LeBron", "limit": 3},
                "get_scoreboard": {"date": "20250101"},
                "find_game": {"team1": "Lakers"},
                "get_game_details": {"game_id": "0022500001"},
                "get_box_score": {"game_id": "0022500001"},
                "get_player_info": {"player": "2544"},
                "get_player_stats": {"player": "2544", "stat_type": "season", "season": "2024-25"},
                "get_player_awards": {"player": "2544"},
                "get_shooting_data": {"player": "2544", "data_type": "splits", "season": "2024-25"},
                "get_team_roster": {"team": "1610612747", "season": "2024-25"},
                "get_standings": {"season": "2024-25"},
                "get_leaders": {"category": "points", "scope": "current_season", "season": "2024-25", "limit": 5},
                "get_schedule": {"team": "1610612747", "days_ahead": 7},
                "get_season_awards": {"season": "2015-16"},
                "get_team_advanced_stats": {"team": "1610612747", "season": "2024-25"},
                "get_play_by_play": {"game_id": "0022500001", "start_period": 1, "end_period": 1},
                "get_game_rotation": {"game_id": "0022500001"},
                "compare_players": {"player1": "2544", "player2": "201939", "season": "2024-25"},
                "daily_summary": {},
                "team_overview": {"team": "1610612747", "season": "2024-25"},
            }

            for name in tool_names:
                args = sample_args.get(name, {})
                fn = getattr(srv, name)
                payload = _as_json(await fn(**args))
                assert payload["tool_name"] == name, f"Tool {name} returned wrong tool_name"
                assert payload["schema_version"] == "3.0", f"Tool {name} has wrong schema_version"
