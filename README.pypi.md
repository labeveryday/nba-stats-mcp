# NBA MCP Server

Access NBA statistics via the Model Context Protocol (MCP).

This package runs an **MCP server** with **21 consolidated tools** — live scores, box scores, standings, player/team stats, play-by-play, shot charts, and more. All tools accept **human names** (not just IDs), return **structured data + compact text**, and default season to current.

Supports **stdio**, **streamable HTTP**, and **SSE** transports. No API key required.

## Quick Start

### With uvx (Recommended - No Install Required)

Add to your MCP client config (e.g., Claude Desktop):

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

Restart your client and start asking!

### With pip

```bash
pip install nba-stats-mcp
```

Then configure your MCP client:

```json
{
  "mcpServers": {
    "nba-stats": {
      "command": "nba-stats-mcp"
    }
  }
}
```

## Response Format (v3.0)

All tools return **JSON** (encoded in the MCP `TextContent.text` field). Each response includes:
- `text` — compact 1-3 line summary (clean, no IDs or URLs)
- `data` — structured dict with all values (primary output for programmatic use)
- `entities` — extracted IDs + asset URLs for UI rendering

Example:

```json
{
  "tool_name": "get_player_stats",
  "arguments": {"player": "LeBron James", "stat_type": "season"},
  "text": "LeBron James 2025-26: 25.3 PPG, 7.1 RPG, 7.8 APG (51.2% FG)",
  "data": {
    "player_id": 2544, "name": "LeBron James", "season": "2025-26",
    "pts": 25.3, "reb": 7.1, "ast": 7.8, "fg_pct": 0.512
  },
  "entities": {
    "players": [{"player_id": "2544", "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/2544.png"}]
  }
}
```

## What You Can Ask

- "Show me today's NBA games"
- "What are LeBron James' stats this season?"
- "Compare LeBron and Curry"
- "Give me a team overview for the Lakers"
- "Who are the top 10 scorers this season?"
- "Show me all-time assists leaders"
- "When do the Celtics play next?"

## Features

**21 consolidated tools** (optimized for LLM clients):
- All tools accept **names** — `get_player_stats(player="LeBron James")` works
- Structured `data` field for programmatic access
- 3 new **composite tools**: `compare_players`, `daily_summary`, `team_overview`
- Live game scores and play-by-play
- Player stats (season, career, game log, hustle, defense, advanced)
- Team rosters and advanced metrics
- League standings and leaders (current season, all-time, hustle)
- Shot charts and shooting analytics

[Full Documentation & Tool Reference](https://github.com/labeveryday/nba-stats-mcp)

## Requirements

- Python 3.10+
- An MCP-compatible client

## License

MIT License - See [LICENSE](https://github.com/labeveryday/nba-stats-mcp/blob/main/LICENSE) for details.
