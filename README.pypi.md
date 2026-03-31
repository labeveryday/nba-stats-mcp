# 🏀 NBA MCP Server

Access NBA statistics via the Model Context Protocol (MCP).

This package runs an **MCP server** that exposes **30 NBA tools** (live scores, box scores, standings, player/team stats, play-by-play, shot charts, etc.).
Supports **stdio**, **streamable HTTP**, and **SSE** transports. Designed for **agents and UIs** that want structured outputs.

**No API key required.**

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

## What you get back (JSON + images)

All tools return **JSON** (encoded in the MCP `TextContent.text` field). Each response includes:
- `tool_name`, `arguments`
- `text` (a readable summary)
- `entities` (extracted IDs + asset URLs for easy UI rendering)

Example (trimmed):

```json
{
  "tool_name": "resolve_player_id",
  "arguments": {"query": "LeBron", "limit": 5},
  "text": "Player ID matches for 'LeBron': ...",
  "entities": {
    "players": [
      {
        "player_id": "2544",
        "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/2544.png",
        "thumbnail_url": "https://cdn.nba.com/headshots/nba/latest/260x190/2544.png"
      }
    ],
    "teams": [],
    "games": []
  }
}
```

### Visual assets (public NBA CDN)

Tool responses include public CDN URLs (no API key required):
- Player headshots (1040x760 + 260x190 thumbnails)
- Team logos (SVG)

## What You Can Ask

- "Show me today's NBA games"
- "What are LeBron James' stats this season?"
- "Get the box score for Lakers vs Warriors"
- "Who are the top 10 scorers this season?"
- "Show me all-time assists leaders"
- "When do the Celtics play next?"
- "Get Stephen Curry's shot chart"
- "Show me Giannis' career awards"

## Features

**30 comprehensive tools** providing access to:
- Live game scores and play-by-play
- Player stats, career data, and awards
- Team rosters and advanced metrics
- League standings and leaders
- Shot charts and shooting analytics
- Historical NBA data

📖 **[Full Documentation & Tool Reference →](https://github.com/labeveryday/nba-stats-mcp)**

## Requirements

- Python 3.10+
- An MCP-compatible client

## License

MIT License - See [LICENSE](https://github.com/labeveryday/nba-stats-mcp/blob/main/LICENSE) for details.
