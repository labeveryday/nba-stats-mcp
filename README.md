# 🏀 NBA MCP Server

[![PyPI version](https://badge.fury.io/py/nba-stats-mcp.svg)](https://badge.fury.io/py/nba-stats-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](https://github.com/labeveryday/nba-stats-mcp)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

Access comprehensive NBA statistics via Model Context Protocol

A Model Context Protocol (MCP) server that provides access to live and historical NBA data including player stats, game scores, team information, and advanced analytics. **v0.3.0**: All 21 tools accept human names (not just IDs), return structured data + compact text, and default season to current. Optimized for both large and small LLM clients.

## Quick Start with Claude Desktop

1. Install the server:
```bash
# Using uvx (recommended - no install required)
uvx nba-stats-mcp

# Or using pip
pip install nba-stats-mcp

# Or from source
git clone https://github.com/labeveryday/nba-stats-mcp.git
cd nba-stats-mcp
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
        "/absolute/path/to/nba-stats-mcp/",
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

## Available Tools (21 total)

All tools accept **human names** — no need to resolve IDs first. Every response includes structured `data` + compact `text`.

### Player Tools
- `get_player_info(player)` - Player bio and details (accepts name or ID)
- `get_player_stats(player, stat_type)` - Season, career, game log, hustle, defense, or advanced stats
- `get_player_awards(player)` - All awards and accolades
- `get_shooting_data(player, data_type)` - Shot chart or shooting splits

### Team Tools
- `get_team_roster(team)` - Team roster (accepts team name or ID)
- `get_team_advanced_stats(team)` - Team efficiency metrics (ORtg, DRtg, pace)
- `get_schedule(team)` - Upcoming games
- `get_standings()` - League standings by conference

### Game Tools
- `get_scoreboard(date?)` - Games for a date (defaults to today)
- `find_game(team1, team2?, date?)` - Find game_id by team matchup
- `get_game_details(game_id)` - Live game info with team stats
- `get_box_score(game_id)` - Full box score with player stats
- `get_play_by_play(game_id)` - Play-by-play with timestamps
- `get_game_rotation(game_id)` - Player rotation/substitution data

### League Tools
- `get_leaders(category, scope)` - Current season, all-time, or hustle leaders
- `get_season_awards(season?)` - Season MVP and major awards

### Composite Tools (new in v0.3.0)
- `compare_players(player1, player2)` - Side-by-side player comparison
- `daily_summary(date?)` - All games + scores for a date
- `team_overview(team)` - Roster + record + upcoming schedule

### Resolution Tools
- `resolve_player_id(query)` - Fuzzy match player name to ID
- `resolve_team_id(query)` - Fuzzy match team name to ID

## Visual Assets (Public NBA CDN)

This MCP server also returns **public NBA CDN asset URLs** (no API key) alongside IDs in several tool responses, so UI clients can render visuals.

- **Player headshots**:
  - Full size: `https://cdn.nba.com/headshots/nba/latest/1040x760/{playerId}.png`
  - Thumbnail: `https://cdn.nba.com/headshots/nba/latest/260x190/{playerId}.png`
- **Team logos (SVG)**:
  - `https://cdn.nba.com/logos/nba/{teamId}/global/L/logo.svg`

Tools that include these URLs:
- **players**: `resolve_player_id`, `get_player_info`, `get_player_stats`
- **teams**: `resolve_team_id`, `get_standings`, `get_team_roster`

## Installation Options

### With uv (recommended)
```bash
git clone https://github.com/labeveryday/nba-stats-mcp.git
cd nba-stats-mcp
uv sync
```

### With pip
```bash
pip install nba-stats-mcp
```

### From source
```bash
git clone https://github.com/labeveryday/nba-stats-mcp.git
cd nba-stats-mcp
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
# If installed via pip/uvx (stdio, default)
nba-stats-mcp

# Or from source
uv run nba-stats-mcp
# or
python -m nba_mcp_server
```

### Streamable HTTP Transport (new in v0.2.0)
```bash
# Run as an HTTP server instead of stdio
nba-stats-mcp --transport streamable-http --host 127.0.0.1 --port 8000

# Also supports SSE transport
nba-stats-mcp --transport sse --port 8000
```

### MCP Inspector
```bash
npx @modelcontextprotocol/inspector
# In the Inspector UI, configure a stdio server:
# - Command: uv
# - Args: --directory /absolute/path/to/nba-stats-mcp run nba-stats-mcp
#   (or Command: python, Args: -m nba_mcp_server)
```

## JSON Response Format (v3.0)

All tools return a **single JSON object** (encoded as the MCP `TextContent.text` string). The top-level schema is:

- **`tool_name`**: tool that ran
- **`arguments`**: arguments passed
- **`text`**: compact 1-3 line summary (clean — no IDs or CDN URLs)
- **`data`**: structured dict with all machine-readable values (primary output for programmatic use)
- **`entities`**: extracted IDs + asset URLs for UI rendering

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
- mcp >= 1.23.0
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
