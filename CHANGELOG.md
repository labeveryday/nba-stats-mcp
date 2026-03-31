# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-03-30

### Changed
- **Migrated to FastMCP** (`mcp.server.fastmcp.FastMCP`) from low-level `mcp.server.Server`
  - Tools are now `@mcp.tool()` decorated functions with auto-generated schemas
  - Eliminated manual `Tool()` definitions and if/elif dispatch chain
  - ~28% reduction in server.py line count
- **Added streamable-http and SSE transport support** via `--transport` CLI flag
  - `nba-stats-mcp --transport streamable-http --port 8000`
  - `nba-stats-mcp --transport sse --port 8000`
  - stdio remains the default for backward compatibility
- **Unified naming to `nba-stats-mcp`** across server name, logger, and repo URLs
- Bumped `mcp` dependency minimum to `>=1.23.0`

## [0.1.6] - 2026-01-03

### Added
- **Public NBA CDN asset URLs** included in tool responses for UI clients:
  - Player headshots (1040x760 + 260x190 thumbnails)
  - Team logos (SVG)
- **JSON-first tool responses** across the full server toolset (no legacy text-only responses)
- **Tool completeness checks** in the test suite to ensure no advertised tool returns `Unknown tool`

### Changed
- `get_league_leaders` now uses `leaguedashplayerstats` (PerGame) for current-season leaders

## [0.1.5] - 2025-12-14

### Added
- **Runtime improvements for agent workloads**
  - Non-blocking HTTP execution (no event-loop blocking on network calls)
  - Concurrency limiting, retries with backoff, and TTL caching
  - `get_server_info` tool for diagnostics
  - `resolve_team_id`, `resolve_player_id`, `find_game_id` helper tools for agent ergonomics
- **Better local usage and demos**
  - `python -m nba_mcp_server` support (`__main__.py`)
  - Strands example agent with interactive REPL and `--demo` mode (`examples/strands_nba_agent.py`)
  - Live end-to-end MCP tool smoke test (`scripts/live_mcp_tool_smoke_test.py`)
- **Tooling**
  - Ruff + Bandit configuration in `pyproject.toml`
  - Release tooling (`build`, `twine`) included in dev dependencies

### Changed
- Server now exposes **30 tools**
- Live scoreboard endpoints may return 403 in some environments; server falls back to stats API scoreboard for reliability

### Fixed
- MCP live smoke test no longer “hangs” (proper MCP session lifecycle + better progress output)
- Server startup is more resilient in environments where CA bundles are not readable (`NBA_MCP_TLS_VERIFY`)

### Added
- **Awards Tools**: Two new tools for accessing NBA awards and accolades
  - `get_player_awards` - Get all awards for a specific player (MVP, Championships, All-Star, All-NBA, etc.)
  - `get_season_awards` - Get major award winners for a specific season
- `get_player_game_log` - Get game-by-game statistics showing highest-scoring games
- Comprehensive endpoint verification system
- ENDPOINT_VERIFICATION.md report documenting all working endpoints
- Verification script to test all tools with real API calls
- Test coverage for new awards tools (4 new tests, 29 total)
- Configurable logging via `NBA_MCP_LOG_LEVEL` environment variable (defaults to WARNING for production)

### Fixed
- `get_player_season_stats` now uses `playercareerstats` endpoint (fixes 500 errors)
- `get_league_leaders` now uses `leaguedashplayerstats` endpoint (fixes 500 errors and returns true per-game leaders)

### Changed
- Server now exposes 20 tools (was 18)
- All endpoints verified working and production-ready

## [0.1.0] - 2025-11-03

### Added
- Initial release of NBA MCP Server
- 17 tools for comprehensive NBA data access:
  - **Live Game Tools**: Today's scoreboard, scoreboard by date, game details, box scores
  - **Player Tools**: Player search, player info, season stats, career stats, hustle stats, defense stats
  - **Team Tools**: List all teams, team rosters
  - **League Tools**: Standings, league leaders, all-time leaders, hustle leaders, team schedules
- Direct HTTP API integration with NBA's official endpoints
- Support for live game data, historical statistics, and future schedules
- Real-time score updates and player-by-player stats
- Advanced statistics including hustle stats and defensive impact
- Comprehensive test suite (25 tests)
- GitHub Actions CI/CD pipeline
- PyPI-ready package structure

### Technical
- Single-module implementation for simplicity
- Proper error handling and fallback strategies
- Support for Python 3.10+
- Uses MCP SDK 1.0.0+ and httpx for HTTP requests
- Pytest with coverage reporting
- Multi-platform CI testing (Ubuntu, macOS, Windows)

[0.1.0]: https://github.com/labeveryday/nba_mcp_server/releases/tag/v0.1.0
[0.1.5]: https://github.com/labeveryday/nba_mcp_server/releases/tag/v0.1.5
