# 🏀 NBA MCP Server

[![PyPI version](https://badge.fury.io/py/nba-stats-mcp.svg)](https://badge.fury.io/py/nba-stats-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](https://github.com/labeveryday/nba_mcp_server)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

Access comprehensive NBA statistics via Model Context Protocol

A Model Context Protocol (MCP) server that provides access to live and historical NBA data including player stats, game scores, team information, and advanced analytics.

## Quick Start with Claude Desktop

1. Install the server:
```bash
# Using uvx (recommended - no install required)
uvx nba-stats-mcp

# Or using pip
pip install nba-stats-mcp

# Or from source
git clone https://github.com/labeveryday/nba_mcp_server.git
cd nba_mcp_server
uv sync
```

2. Add to your Claude Desktop config file:

**MacOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "nba-stats": {
      "command": "uvx",
      "args": ["nba-stats-mcp"]
    }
  }
}
```

Or if you installed from source:
```json
{
  "mcpServers": {
    "nba-stats": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/nba_mcp_server/",
        "run",
        "nba-stats-mcp"
      ]
    }
  }
}
```

3. Restart Claude Desktop

## Hosted deployment

A hosted deployment is available on [Fronteir AI](https://fronteir.ai/mcp/labeveryday-nba-mcp-server).

## What You Can Ask

- "Show me today's NBA games"
- "What are LeBron James' stats this season?"
- "Get the box score for Lakers vs Warriors"
- "Who are the top 10 scorers this season?"
- "Show me all-time assists leaders"
- "When do the Celtics play next?"
- "Get Stephen Curry's shot chart"
- "Who are the league leaders in deflections?"
- "Show me Giannis' career awards"

## Available Tools (30 total)

### Server Utilities
- `get_server_info` - Server version + runtime settings (timeouts, retries, cache, concurrency)
- `resolve_team_id` - Resolve team name/city/nickname → team_id
- `resolve_player_id` - Resolve player name → player_id (official stats endpoint)
- `find_game_id` - Find game_id by date + matchup filters

### Player Stats
- `search_players` - Find players by name
- `get_player_info` - Player bio and details
- `get_player_season_stats` - Current/historical season stats
- `get_player_career_stats` - Career totals and averages
- `get_player_game_log` - Game-by-game performance
- `get_player_awards` - All awards and accolades
- `get_player_hustle_stats` - Deflections, charges, loose balls, box outs
- `get_player_defense_stats` - Opponent FG% when defended
- `get_player_advanced_stats` - TS%, ORtg, DRtg, USG%, PIE

### Team Stats
- `get_all_teams` - All 30 NBA teams
- `get_team_roster` - Team roster
- `get_team_advanced_stats` - Team efficiency metrics

### Live Games
- `get_todays_scoreboard` - Today's games with live scores
- `get_scoreboard_by_date` - Games for specific date
- `get_game_details` - Detailed game info with live stats
- `get_box_score` - Full box score with player stats
- `get_play_by_play` - Complete play-by-play data
- `get_game_rotation` - Player substitution patterns

### League Stats
- `get_standings` - Current NBA standings
- `get_league_leaders` - Statistical leaders (PTS, AST, REB, etc.)
- `get_all_time_leaders` - All-time career leaders
- `get_league_hustle_leaders` - League leaders in hustle stats
- `get_schedule` - Team schedule (up to 90 days ahead)
- `get_season_awards` - Season MVP and major awards

### Shooting Analytics
- `get_shot_chart` - Shot locations with X/Y coordinates
- `get_shooting_splits` - Shooting % by zone and distance

## Visual Assets (Public NBA CDN)

This MCP server also returns **public NBA CDN asset URLs** (no API key) alongside IDs in several tool responses, so UI clients can render visuals.

- **Player headshots**:
  - Full size: `https://cdn.nba.com/headshots/nba/latest/1040x760/{playerId}.png`
  - Thumbnail: `https://cdn.nba.com/headshots/nba/latest/260x190/{playerId}.png`
- **Team logos (SVG)**:
  - `https://cdn.nba.com/logos/nba/{teamId}/global/L/logo.svg`

Tools that include these URLs:
- **players**: `resolve_player_id`, `search_players`, `get_player_info`
- **teams**: `resolve_team_id`, `get_all_teams`, `get_standings`

## Installation Options

### With uv (recommended)
```bash
git clone https://github.com/labeveryday/nba_mcp_server.git
cd nba_mcp_server
uv sync
```

### With pip
```bash
pip install nba-stats-mcp
```

### From source
```bash
git clone https://github.com/labeveryday/nba_mcp_server.git
cd nba_mcp_server
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

## Usage with Other MCP Clients

### Python/Strands
```python
from mcp import stdio_client, StdioServerParameters
from strands.tools.mcp import MCPClient

mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(
        command="uvx",
        args=["nba-stats-mcp"]
    )
))
```

### Running Standalone (for testing)
```bash
# If installed via pip/uvx
nba-stats-mcp

# Or from source
uv run nba-stats-mcp
# or
python -m nba_mcp_server

# or Test with MCP Inspector
# (Inspector launches a stdio server command; it is NOT the python module name.)
npx @modelcontextprotocol/inspector
# In the Inspector UI, configure a stdio server:
# - Command: uv
# - Args: --directory /absolute/path/to/nba_mcp_server run nba-stats-mcp
#   (or Command: python, Args: -m nba_mcp_server)
```

## JSON Response Format

All tools return a **single JSON object** (encoded as the MCP `TextContent.text` string). The top-level schema is:

- **`tool_name`**: tool that ran
- **`arguments`**: arguments passed
- **`text`**: human-readable summary (kept for debugging and display)
- **`entities`**: machine-friendly IDs + asset URLs extracted from the result

### Visual Assets (Public NBA CDN)

The server includes public CDN URLs (no API key required) in `entities`:

- **Player headshots**:
  - `headshot_url`: `https://cdn.nba.com/headshots/nba/latest/1040x760/{playerId}.png`
  - `thumbnail_url`: `https://cdn.nba.com/headshots/nba/latest/260x190/{playerId}.png`
- **Team logos**:
  - `team_logo_url`: `https://cdn.nba.com/logos/nba/{teamId}/global/L/logo.svg`

## Configuration

### Logging Levels

Control logging verbosity with the `NBA_MCP_LOG_LEVEL` environment variable (default: WARNING):

```bash
export NBA_MCP_LOG_LEVEL=INFO  # For debugging
nba-stats-mcp
```

In Claude Desktop config:
```json
{
  "mcpServers": {
    "nba-stats": {
      "command": "uvx",
      "args": ["nba-stats-mcp"],
      "env": {
        "NBA_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Performance & Reliability Tuning

You can tune request behavior (helpful when agents do parallel tool calls) via env vars:

- **`NBA_MCP_HTTP_TIMEOUT_SECONDS`**: Per-request timeout (default: `30`)
- **`NBA_MCP_MAX_CONCURRENCY`**: Max concurrent outbound NBA API requests (default: `8`)
- **`NBA_MCP_RETRIES`**: Retries for transient failures (429 / 5xx / network) (default: `2`)
- **`NBA_MCP_CACHE_TTL_SECONDS`**: Cache TTL for stats endpoints (default: `120`)
- **`NBA_MCP_LIVE_CACHE_TTL_SECONDS`**: Cache TTL for live endpoints (default: `5`)
- **`NBA_MCP_TLS_VERIFY`**: TLS verification enabled (default: `1`). If you see `PermissionError` reading CA bundles (common in sandboxed/macOS privacy contexts), set to `0`.

Example Claude Desktop config:

```json
{
  "mcpServers": {
    "nba-stats": {
      "command": "uvx",
      "args": ["nba-stats-mcp"],
      "env": {
        "NBA_MCP_LOG_LEVEL": "INFO",
        "NBA_MCP_MAX_CONCURRENCY": "8",
        "NBA_MCP_CACHE_TTL_SECONDS": "120",
        "NBA_MCP_LIVE_CACHE_TTL_SECONDS": "5",
        "NBA_MCP_RETRIES": "2",
        "NBA_MCP_HTTP_TIMEOUT_SECONDS": "30"
      }
    }
  }
}
```

## Data Sources

This server uses official NBA APIs:
- **Live Data API** - Real-time scores and game data
- **Stats API** - Player stats, team info, historical data
- **Schedule API** - Full season schedule including future games

## Development

### Running Tests
```bash
uv sync --all-extras
uv run pytest
uv run pytest --cov=nba_mcp_server --cov-report=html
```

### Code Quality
```bash
uv run ruff check src/
uv run ruff format src/
```

### Security (Bandit)

Static security analysis:

```bash
uv sync --all-extras
uv run bandit -c pyproject.toml -r src/
```

## Releasing to PyPI

This project uses Hatchling for builds. Recommended release steps:

```bash
# 1) Ensure clean env + tests
uv sync --all-extras
uv run pytest
uv run ruff check src/ tests/
uv run bandit -c pyproject.toml -r src/

# 2) Build distributions
uv run python -m build

# 3) Upload
uv run twine upload dist/*
```

Tip: for TestPyPI uploads, use `twine upload --repository testpypi dist/*`.

## Requirements

- Python 3.10+
- mcp >= 1.0.0
- httpx >= 0.27.0

## License

MIT License - see LICENSE file for details.

## Contributing

Contributions welcome! Please submit a Pull Request.

## About the Author

>This project was created by **Du'An Lightfoot**, a developer passionate about AI agents, cloud infrastructure, and teaching in public.
>
>Learn more and connect:
>- 🌐 Website: [duanlightfoot.com](https://duanlightfoot.com)
>- 📺 YouTube: [@LabEveryday](https://www.youtube.com/@LabEveryday)
>- 🐙 GitHub: [@labeveryday](https://github.com/labeveryday)
