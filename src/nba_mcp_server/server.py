#!/usr/bin/env python3
"""
NBA MCP Server (JSON-first)

This server exposes the full NBA toolset (30 tools) but returns JSON in every response so
agents/frontends can parse results reliably.

Response shape (always JSON string in TextContent.text):
{
  "entity_type": "tool_result",
  "schema_version": "2.0",
  "tool_name": "...",
  "arguments": {...},
  "text": "...",            # legacy human-readable text (kept for robustness / debugging)
  "entities": {...},        # best-effort extracted ids + CDN asset URLs
  "error": "..."            # only when errors occur
}
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import random
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

# Configure logging - default to WARNING for production, can be overridden with NBA_MCP_LOG_LEVEL
log_level = os.getenv("NBA_MCP_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.WARNING),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("nba-stats-mcp")

# NBA API endpoints
NBA_LIVE_API = "https://cdn.nba.com/static/json/liveData"
NBA_STATS_API = "https://stats.nba.com/stats"

# NBA public CDN assets (no API key required)
NBA_CDN_HEADSHOTS_BASE = "https://cdn.nba.com/headshots/nba/latest"
NBA_CDN_LOGOS_BASE = "https://cdn.nba.com/logos/nba"


def get_player_headshot_url(player_id: Any, size: str = "1040x760") -> str:
    pid = str(player_id).strip()
    return f"{NBA_CDN_HEADSHOTS_BASE}/{size}/{pid}.png"


def get_player_headshot_thumbnail_url(player_id: Any) -> str:
    return get_player_headshot_url(player_id, size="260x190")


def get_team_logo_url(team_id: Any) -> str:
    tid = str(team_id).strip()
    return f"{NBA_CDN_LOGOS_BASE}/{tid}/global/L/logo.svg"


# Hardcoded team mapping (fast + reliable; also used by resolver tools)
NBA_TEAMS: dict[int, str] = {
    1610612737: "Atlanta Hawks",
    1610612738: "Boston Celtics",
    1610612751: "Brooklyn Nets",
    1610612766: "Charlotte Hornets",
    1610612741: "Chicago Bulls",
    1610612739: "Cleveland Cavaliers",
    1610612742: "Dallas Mavericks",
    1610612743: "Denver Nuggets",
    1610612765: "Detroit Pistons",
    1610612744: "Golden State Warriors",
    1610612745: "Houston Rockets",
    1610612754: "Indiana Pacers",
    1610612746: "LA Clippers",
    1610612747: "Los Angeles Lakers",
    1610612763: "Memphis Grizzlies",
    1610612748: "Miami Heat",
    1610612749: "Milwaukee Bucks",
    1610612750: "Minnesota Timberwolves",
    1610612740: "New Orleans Pelicans",
    1610612752: "New York Knicks",
    1610612760: "Oklahoma City Thunder",
    1610612753: "Orlando Magic",
    1610612755: "Philadelphia 76ers",
    1610612756: "Phoenix Suns",
    1610612757: "Portland Trail Blazers",
    1610612758: "Sacramento Kings",
    1610612759: "San Antonio Spurs",
    1610612761: "Toronto Raptors",
    1610612762: "Utah Jazz",
    1610612764: "Washington Wizards",
}

# Standard headers for NBA API requests
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}

# Create server instance
mcp = FastMCP("nba-stats-mcp")


# ==================== Runtime Configuration ====================


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# HTTP client config
NBA_MCP_HTTP_TIMEOUT_SECONDS = _env_float("NBA_MCP_HTTP_TIMEOUT_SECONDS", 30.0)
NBA_MCP_MAX_CONCURRENCY = max(1, _env_int("NBA_MCP_MAX_CONCURRENCY", 8))
NBA_MCP_RETRIES = max(0, _env_int("NBA_MCP_RETRIES", 2))
NBA_MCP_CACHE_TTL_SECONDS = max(0.0, _env_float("NBA_MCP_CACHE_TTL_SECONDS", 120.0))
NBA_MCP_LIVE_CACHE_TTL_SECONDS = max(0.0, _env_float("NBA_MCP_LIVE_CACHE_TTL_SECONDS", 5.0))

# TLS verification (default on).
NBA_MCP_TLS_VERIFY = os.getenv("NBA_MCP_TLS_VERIFY", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# HTTP client (sync) + lazy init
http_client: Any = None


def _get_http_client() -> httpx.Client:
    global http_client
    if http_client is not None:
        return http_client

    try:
        http_client = httpx.Client(
            timeout=NBA_MCP_HTTP_TIMEOUT_SECONDS,
            headers=NBA_HEADERS,
            follow_redirects=True,
            verify=NBA_MCP_TLS_VERIFY,
        )
        return http_client
    except PermissionError as e:
        if NBA_MCP_TLS_VERIFY:
            logger.warning(
                "Permission error initializing TLS verification (CA bundle not readable). "
                "Falling back to system default SSLContext (still verifies TLS). "
                "If you must disable TLS verification (NOT recommended), set NBA_MCP_TLS_VERIFY=0. "
                f"Error: {e}",
            )
            ctx = ssl.create_default_context()
            http_client = httpx.Client(
                timeout=NBA_MCP_HTTP_TIMEOUT_SECONDS,
                headers=NBA_HEADERS,
                follow_redirects=True,
                verify=ctx,
            )
            return http_client

        http_client = httpx.Client(
            timeout=NBA_MCP_HTTP_TIMEOUT_SECONDS,
            headers=NBA_HEADERS,
            follow_redirects=True,
            verify=False,  # nosec B501
        )
        return http_client


# Bound concurrent outbound requests so agents can safely parallelize calls.
_request_semaphore: Optional[asyncio.Semaphore] = None


def _get_request_semaphore() -> asyncio.Semaphore:
    global _request_semaphore
    if _request_semaphore is None:
        _request_semaphore = asyncio.Semaphore(NBA_MCP_MAX_CONCURRENCY)
    return _request_semaphore


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: dict


_cache: dict[str, _CacheEntry] = {}
_cache_lock: Optional[asyncio.Lock] = None
# Negative cache for known-unavailable requests (e.g., live scoreboard 403 in some environments).
_negative_cache: dict[str, float] = {}


def _get_cache_lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _cache_ttl_for_url(url: str) -> float:
    if url.startswith(NBA_LIVE_API):
        return NBA_MCP_LIVE_CACHE_TTL_SECONDS
    return NBA_MCP_CACHE_TTL_SECONDS


def _cache_key(url: str, params: Optional[dict]) -> str:
    if not params:
        return url
    items = sorted((str(k), str(v)) for k, v in params.items())
    return f"{url}?{json.dumps(items, separators=(',', ':'), ensure_ascii=True)}"


def _team_name_from_id(team_id: Any) -> str:
    try:
        tid = int(team_id)
    except Exception:
        return str(team_id)
    return NBA_TEAMS.get(tid, str(tid))


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _best_team_id_from_query(query: str) -> Optional[int]:
    q = str(query or "").strip().lower()
    if not q:
        return None
    best: tuple[float, int] | None = None
    for tid, name in NBA_TEAMS.items():
        name_l = name.lower()
        if q in name_l:
            score = 1.0
        else:
            score = difflib.SequenceMatcher(None, q, name_l).ratio()
        if best is None or score > best[0]:
            best = (score, tid)
    if not best or best[0] < 0.3:
        return None
    return best[1]


# ==================== Helper Functions ====================


def safe_get(data: Any, *keys, default="N/A"):
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list):
            try:
                if isinstance(key, int) and 0 <= key < len(data):
                    data = data[key]
                else:
                    return default
            except (TypeError, IndexError):
                return default
        else:
            return default
        if data is None:
            return default
    return data if data != "" else default


def format_stat(value: Any, is_percentage: bool = False) -> str:
    if value is None or value == "":
        return "N/A"
    try:
        num = float(value)
        if is_percentage:
            return f"{num * 100:.1f}%"
        return f"{num:.1f}"
    except (ValueError, TypeError):
        return str(value)


async def fetch_nba_data(url: str, params: Optional[dict] = None) -> Optional[dict]:
    ttl = _cache_ttl_for_url(url)
    key = _cache_key(url, params)

    if ttl > 0:
        now = time.monotonic()
        async with _get_cache_lock():
            neg_exp = _negative_cache.get(key)
            if neg_exp and neg_exp > now:
                logger.debug(f"Negative cache hit for {url}")
                return None
            if neg_exp:
                _negative_cache.pop(key, None)

            entry = _cache.get(key)
            if entry and entry.expires_at > now:
                logger.debug(f"Cache hit for {url}")
                return entry.value
            if entry:
                _cache.pop(key, None)

    attempt = 0
    last_error: Optional[Exception] = None

    while attempt <= NBA_MCP_RETRIES:
        try:
            client = _get_http_client()
            async with _get_request_semaphore():
                response = await asyncio.to_thread(client.get, url, params=params)
            response.raise_for_status()
            data = response.json()

            if ttl > 0:
                async with _get_cache_lock():
                    _cache[key] = _CacheEntry(expires_at=time.monotonic() + ttl, value=data)
            return data

        except httpx.HTTPStatusError as e:
            last_error = e
            status = getattr(e.response, "status_code", None)
            if status in (429,) or (isinstance(status, int) and status >= 500):
                if attempt >= NBA_MCP_RETRIES:
                    break
                retry_after = None
                try:
                    ra = e.response.headers.get("Retry-After")
                    if ra:
                        retry_after = float(ra)
                except Exception:
                    retry_after = None
                delay = (
                    retry_after
                    if retry_after is not None
                    else (0.5 * (2**attempt)) + random.random() * 0.2  # nosec B311
                )
                logger.warning(
                    f"HTTP {status} from NBA API; retrying in {delay:.2f}s (attempt {attempt + 1}/{NBA_MCP_RETRIES})",
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if status == 403 and url.startswith(NBA_LIVE_API):
                # Expected in some environments; callers usually have a stats API fallback.
                logger.info(f"HTTP 403 from live NBA endpoint (will fall back): {url}")
                neg_ttl = max(10.0, float(ttl or 0.0))
                async with _get_cache_lock():
                    _negative_cache[key] = time.monotonic() + neg_ttl
                return None

            logger.error(f"HTTP status error fetching {url}: {e}")
            return None

        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            if attempt >= NBA_MCP_RETRIES:
                break
            delay = (0.5 * (2**attempt)) + random.random() * 0.2  # nosec B311
            logger.warning(
                f"Network error from NBA API; retrying in {delay:.2f}s (attempt {attempt + 1}/{NBA_MCP_RETRIES}): {e}",
            )
            await asyncio.sleep(delay)
            attempt += 1
            continue

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for {url}: {e}")
            return None

        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            return None

    logger.error(f"Failed fetching {url} after retries: {last_error}")
    return None


async def clear_cache() -> None:
    async with _get_cache_lock():
        _cache.clear()
        _negative_cache.clear()


def get_current_season() -> str:
    now = datetime.now()
    year = now.year
    if now.month >= 10:
        return f"{year}-{str(year + 1)[2:]}"
    return f"{year - 1}-{str(year)[2:]}"


async def _get_scoreboard_games_stats_api(date_obj: datetime) -> Optional[list[dict[str, Any]]]:
    """
    Fallback scoreboard source via stats.nba.com (scoreboardv2).
    Returns: {game_id, home_name, away_name, home_score, away_score, status}
    """
    url = f"{NBA_STATS_API}/scoreboardv2"
    params = {"GameDate": date_obj.strftime("%m/%d/%Y"), "LeagueID": "00", "DayOffset": "0"}
    data = await fetch_nba_data(url, params)
    if not data:
        return None

    result_sets = safe_get(data, "resultSets", default=[])
    if not result_sets or result_sets == "N/A":
        return None

    game_header = None
    line_score = None
    for rs in result_sets:
        name = safe_get(rs, "name", default="")
        if name == "GameHeader":
            game_header = rs
        elif name == "LineScore":
            line_score = rs

    if not game_header:
        return None

    gh_headers = safe_get(game_header, "headers", default=[])
    gh_rows = safe_get(game_header, "rowSet", default=[])
    if not gh_headers or not gh_rows:
        return []

    def _idx(headers: list, col: str, fallback: int) -> int:
        try:
            return headers.index(col)
        except ValueError:
            return fallback

    gid_idx = _idx(gh_headers, "GAME_ID", 2)
    home_id_idx = _idx(gh_headers, "HOME_TEAM_ID", 6)
    away_id_idx = _idx(gh_headers, "VISITOR_TEAM_ID", 7)
    status_text_idx = _idx(gh_headers, "GAME_STATUS_TEXT", -1)

    scores: dict[tuple[str, int], Any] = {}
    if line_score:
        ls_headers = safe_get(line_score, "headers", default=[])
        ls_rows = safe_get(line_score, "rowSet", default=[])
        if ls_headers and ls_rows:
            ls_gid_idx = _idx(ls_headers, "GAME_ID", 0)
            ls_team_id_idx = _idx(ls_headers, "TEAM_ID", 1)
            ls_pts_idx = _idx(ls_headers, "PTS", -1)
            for row in ls_rows:
                game_id = str(safe_get(row, ls_gid_idx, default=""))
                team_id = _to_int(safe_get(row, ls_team_id_idx, default=0))
                if team_id is None:
                    continue
                pts = safe_get(row, ls_pts_idx, default="N/A") if ls_pts_idx >= 0 else "N/A"
                scores[(game_id, team_id)] = pts

    games: list[dict[str, Any]] = []
    for row in gh_rows:
        game_id = str(safe_get(row, gid_idx, default="N/A"))
        home_id_val = safe_get(row, home_id_idx, default="N/A")
        away_id_val = safe_get(row, away_id_idx, default="N/A")
        try:
            home_id = int(home_id_val)
        except Exception:
            home_id = 0
        try:
            away_id = int(away_id_val)
        except Exception:
            away_id = 0

        status = (
            safe_get(row, status_text_idx, default="Unknown") if status_text_idx >= 0 else "Unknown"
        )
        games.append(
            {
                "game_id": game_id,
                "home_name": _team_name_from_id(home_id),
                "away_name": _team_name_from_id(away_id),
                "home_score": scores.get((game_id, home_id), "N/A"),
                "away_score": scores.get((game_id, away_id), "N/A"),
                "status": status,
            },
        )

    return games


# ==================== JSON Wrapping ====================


def _extract_entities(text: str, arguments: Any) -> dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    players: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = []
    games: list[dict[str, Any]] = []

    if args.get("player_id"):
        pid = str(args["player_id"]).strip()
        players.append(
            {
                "player_id": pid,
                "headshot_url": get_player_headshot_url(pid),
                "thumbnail_url": get_player_headshot_thumbnail_url(pid),
            },
        )
    if args.get("team_id"):
        tid = str(args["team_id"]).strip()
        teams.append({"team_id": tid, "team_logo_url": get_team_logo_url(tid)})
    if args.get("game_id"):
        gid = str(args["game_id"]).strip()
        games.append({"game_id": gid})

    for tid in sorted(set(re.findall(r"\b1610612\d{3}\b", text))):
        teams.append({"team_id": tid, "team_logo_url": get_team_logo_url(tid)})

    for gid in sorted(set(re.findall(r"\b00\d{8}\b|\b00\d{9}\b", text))):
        games.append({"game_id": gid})

    for pid in sorted(set(re.findall(r"\bID:\s*(\d{3,10})\b", text))):
        if pid.startswith("1610612"):
            continue
        players.append(
            {
                "player_id": pid,
                "headshot_url": get_player_headshot_url(pid),
                "thumbnail_url": get_player_headshot_thumbnail_url(pid),
            },
        )

    def _dedupe(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for it in items:
            v = it.get(key)
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(it)
        return out

    return {
        "players": _dedupe(players, "player_id"),
        "teams": _dedupe(teams, "team_id"),
        "games": _dedupe(games, "game_id"),
    }


def _wrap_tool_result(
    *, tool_name: str, arguments: Any, text: str = "", error: Optional[str] = None
) -> list[TextContent]:
    payload: dict[str, Any] = {
        "entity_type": "tool_result",
        "schema_version": "2.0",
        "tool_name": tool_name,
        "arguments": arguments if isinstance(arguments, dict) else {},
    }
    if error:
        payload["error"] = error
    if text is not None:
        payload["text"] = text
        payload["entities"] = _extract_entities(text, arguments)
    return [
        TextContent(
            type="text", text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    ]


# ==================== Tools ====================


async def _tool_impl(tool_name: str, arguments: dict[str, Any], fn) -> list[TextContent]:
    """Execute tool logic, wrap result in JSON envelope, handle errors."""
    try:
        text = await fn()
        return _wrap_tool_result(tool_name=tool_name, arguments=arguments, text=text)
    except Exception as e:
        logger.error(f"Error in {tool_name}: {e}", exc_info=True)
        return _wrap_tool_result(
            tool_name=tool_name, arguments=arguments, error=f"{type(e).__name__}: {e}"
        )


@mcp.tool(description="Server version + runtime settings (timeouts, retries, cache, concurrency).")
async def get_server_info() -> list[TextContent]:
    async def _impl():
        from nba_mcp_server import __version__
        result = "NBA MCP Server Info:\n\n"
        result += f"Version: {__version__}\n"
        result += f"HTTP timeout (s): {NBA_MCP_HTTP_TIMEOUT_SECONDS}\n"
        result += f"Max concurrency: {NBA_MCP_MAX_CONCURRENCY}\n"
        result += f"Retries: {NBA_MCP_RETRIES}\n"
        result += f"Cache TTL (stats, s): {NBA_MCP_CACHE_TTL_SECONDS}\n"
        result += f"Cache TTL (live, s): {NBA_MCP_LIVE_CACHE_TTL_SECONDS}\n"
        result += f"TLS verify enabled: {NBA_MCP_TLS_VERIFY}\n"
        result += f"Log level: {log_level}\n"
        return result
    return await _tool_impl("get_server_info", {}, _impl)


@mcp.tool(description="All 30 NBA teams with IDs and logos.")
async def get_all_teams() -> list[TextContent]:
    async def _impl():
        result = "NBA Teams:\n\n"
        for tid, tname in sorted(NBA_TEAMS.items(), key=lambda x: x[1]):
            result += f"ID: {tid} | {tname} | Logo: {get_team_logo_url(tid)}\n"
        return result
    return await _tool_impl("get_all_teams", {}, _impl)


@mcp.tool(description="Resolve team name/city/nickname to team_id.")
async def resolve_team_id(query: str, limit: int = 5) -> list[TextContent]:
    _args: dict[str, Any] = {"query": query, "limit": limit}

    async def _impl():
        q = query.strip().lower()
        if not q:
            return "Please provide a non-empty team query."
        scored: list[tuple[float, int, str]] = []
        for tid, tname in NBA_TEAMS.items():
            name_l = tname.lower()
            score = 1.0 if q in name_l else difflib.SequenceMatcher(None, q, name_l).ratio()
            scored.append((score, tid, tname))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [s for s in scored if s[0] >= 0.3][: max(1, limit)]
        if not top:
            return f"No teams matched '{query}'. Try a city or nickname (e.g., 'Boston', 'Lakers')."
        result = f"Team ID matches for '{query}':\n\n"
        for score, tid, tname in top:
            result += f"ID: {tid} | {tname} (match: {score:.2f}) | Logo: {get_team_logo_url(tid)}\n"
        return result
    return await _tool_impl("resolve_team_id", _args, _impl)


@mcp.tool(description="Resolve player name to player_id (official stats endpoint).")
async def resolve_player_id(query: str, active_only: bool = False, limit: int = 10) -> list[TextContent]:
    _args: dict[str, Any] = {"query": query, "active_only": active_only, "limit": limit}

    async def _impl():
        query_raw = query.strip()
        q = query_raw.lower()
        if not query_raw:
            return "Please provide a non-empty player query."
        url = f"{NBA_STATS_API}/commonallplayers"
        params = {"LeagueID": "00", "Season": get_current_season(), "IsOnlyCurrentSeason": "0"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching player data. Please try again."
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not rows or rows == "N/A":
            return "No player data returned by the NBA API."
        matches: list[tuple[float, int, str, int]] = []
        for row in rows:
            player_id = _to_int(row[0] if isinstance(row, list) and row else None)
            if player_id is None:
                continue
            player_name = str(row[2]) if len(row) > 2 else ""
            is_active = int(row[11]) if len(row) > 11 and str(row[11]).isdigit() else 1
            if active_only and is_active != 1:
                continue
            name_l = player_name.lower()
            score = 1.0 if q in name_l else difflib.SequenceMatcher(None, q, name_l).ratio()
            if score >= 0.35:
                matches.append((score, player_id, player_name, is_active))
        matches.sort(key=lambda x: (x[3], x[0]), reverse=True)
        top = matches[: max(1, limit)]
        if not top:
            return f"No players matched '{query_raw}'. Try a different spelling or a shorter substring."
        result = f"Player ID matches for '{query_raw}':\n\n"
        for score, pid, name_, is_active in top:
            status = "Active" if is_active == 1 else "Inactive"
            result += (
                f"ID: {pid} | Name: {name_} | Status: {status} (match: {score:.2f}) | "
                f"Headshot: {get_player_headshot_url(pid)} | Thumb: {get_player_headshot_thumbnail_url(pid)}\n"
            )
        return result
    return await _tool_impl("resolve_player_id", _args, _impl)


@mcp.tool(description="Find game_id by date and matchup. If date omitted, finds most recent matchup via schedule.")
async def find_game_id(
    date: str = "", home_team: str = "", away_team: str = "",
    team: str = "", lookback_days: int = 365, limit: int = 10,
) -> list[TextContent]:
    _args: dict[str, Any] = {"date": date, "home_team": home_team, "away_team": away_team, "team": team, "lookback_days": lookback_days, "limit": limit}

    async def _impl():
        date_str = date.strip()
        home_q = home_team.strip().lower()
        away_q = away_team.strip().lower()
        team_q = team.strip().lower()

        if not date_str:
            lb = max(1, min(3650, lookback_days))
            home_id = _best_team_id_from_query(home_q) if home_q else None
            away_id = _best_team_id_from_query(away_q) if away_q else None
            single_id = _best_team_id_from_query(team_q) if team_q else None
            if not ((home_id and away_id) or single_id):
                return "Please provide 'home_team' + 'away_team' or 'team'."
            url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
            data = await fetch_nba_data(url)
            if not data:
                return "Error fetching league schedule. Please try again."
            game_dates = safe_get(data, "leagueSchedule", "gameDates", default=[])
            if not game_dates:
                return "No schedule data available."
            cutoff_date = datetime.now().date()
            matches: list[dict[str, Any]] = []
            for date_entry in game_dates:
                for game in safe_get(date_entry, "games", default=[]):
                    game_dt_str = safe_get(game, "gameDateTimeEst")
                    if game_dt_str == "N/A":
                        continue
                    try:
                        game_dt = datetime.fromisoformat(str(game_dt_str).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    days_back = (cutoff_date - game_dt.date()).days
                    if days_back < 0 or days_back > lb:
                        continue
                    hid = _to_int(safe_get(game, "homeTeam", "teamId"))
                    aid = _to_int(safe_get(game, "awayTeam", "teamId"))
                    if hid is None or aid is None:
                        continue
                    if home_id and away_id:
                        if {hid, aid} != {home_id, away_id}:
                            continue
                    elif single_id:
                        if hid != single_id and aid != single_id:
                            continue
                    status_text = safe_get(game, "gameStatusText", default="Unknown")
                    status_num = _to_int(safe_get(game, "gameStatus", default=None))
                    matches.append({
                        "game_id": safe_get(game, "gameId", default="N/A"),
                        "date": game_dt.strftime("%Y-%m-%d"),
                        "home_team_id": hid, "away_team_id": aid,
                        "home_name": f"{safe_get(game, 'homeTeam', 'teamCity')} {safe_get(game, 'homeTeam', 'teamName')}".strip(),
                        "away_name": f"{safe_get(game, 'awayTeam', 'teamCity')} {safe_get(game, 'awayTeam', 'teamName')}".strip(),
                        "status": status_text, "status_num": status_num,
                    })

            def _sort_key(m: dict[str, Any]) -> tuple[int, str]:
                completed = 1 if m.get("status_num") == 3 or str(m.get("status", "")).lower().startswith("final") else 0
                return (completed, str(m.get("date", "")))
            matches.sort(key=_sort_key, reverse=True)
            top = matches[: max(1, limit)]
            if not top:
                return "No games found in the schedule for the given filters/window."
            result = "Most recent game ID matches:\n\n"
            for g in top:
                result += f"Game ID: {g.get('game_id')}\nDate: {g.get('date')}\n"
                result += f"{g.get('away_name')} @ {g.get('home_name')}\nStatus: {g.get('status')}\n"
                result += f"Home Team ID: {g.get('home_team_id')} | Logo: {get_team_logo_url(g.get('home_team_id'))}\n"
                result += f"Away Team ID: {g.get('away_team_id')} | Logo: {get_team_logo_url(g.get('away_team_id'))}\n\n"
            return result

        try:
            date_obj = datetime.strptime(date_str, "%Y%m%d")
            formatted_date = date_obj.strftime("%Y-%m-%d")
        except ValueError:
            return "Invalid date format. Use YYYYMMDD (e.g., '20241103')"
        url = f"{NBA_LIVE_API}/scoreboard/scoreboard_{date_str}.json"
        data = await fetch_nba_data(url)
        games = safe_get(data, "scoreboard", "games", default=[]) if data else []
        live_ok = bool(games and games != "N/A")
        if not live_ok:
            stats_games = await _get_scoreboard_games_stats_api(date_obj)
            if stats_games is None:
                return f"No data available for {formatted_date}. The NBA APIs may be unavailable or blocked."
            if not stats_games:
                return f"No games found for {formatted_date}."
            filtered_stats = []
            for g in stats_games:
                hn = str(g.get("home_name", "")).lower()
                an = str(g.get("away_name", "")).lower()
                if home_q and home_q not in hn:
                    continue
                if away_q and away_q not in an:
                    continue
                if team_q and team_q not in hn and team_q not in an:
                    continue
                filtered_stats.append(g)
            if not filtered_stats:
                return f"No games matched your filters for {formatted_date}. Try using only 'team' or check spelling."
            result = f"Game ID matches for {formatted_date}:\n\n"
            for g in filtered_stats[: max(1, limit)]:
                result += f"Game ID: {g.get('game_id', 'N/A')}\n{g.get('away_name', 'Away')} @ {g.get('home_name', 'Home')}\nStatus: {g.get('status', 'Unknown')}\n\n"
            return result
        return f"Game ID matches for {formatted_date}:\n\n(Use scoreboard tools for detailed listings.)\n"
    return await _tool_impl("find_game_id", _args, _impl)


@mcp.tool(description="Today's games.")
async def get_todays_scoreboard() -> list[TextContent]:
    async def _impl():
        url = f"{NBA_LIVE_API}/scoreboard/todaysScoreboard_00.json"
        data = await fetch_nba_data(url)
        if not data:
            today = datetime.now()
            stats_games = await _get_scoreboard_games_stats_api(today)
            if stats_games is None:
                return "Error fetching today's scoreboard. Please try again."
            if not stats_games:
                return f"No games scheduled for {today.strftime('%Y-%m-%d')}."
            result = f"NBA Games for {today.strftime('%Y-%m-%d')}:\n\n"
            for g in stats_games:
                result += f"Game ID: {g.get('game_id', 'N/A')}\n{g.get('away_name', 'Away')} ({g.get('away_score', 'N/A')}) @ {g.get('home_name', 'Home')} ({g.get('home_score', 'N/A')})\nStatus: {g.get('status', 'Unknown')}\n\n"
            return result
        scoreboard = safe_get(data, "scoreboard")
        if not scoreboard or scoreboard == "N/A":
            return "No scoreboard data available."
        games = safe_get(scoreboard, "games", default=[])
        game_date = safe_get(scoreboard, "gameDate", default=datetime.now().strftime("%Y-%m-%d"))
        if not games:
            return f"No games scheduled for {game_date}."
        result = f"NBA Games for {game_date}:\n\n"
        for game in games:
            ht = safe_get(game, "homeTeam", default={})
            at = safe_get(game, "awayTeam", default={})
            hid = safe_get(ht, "teamId", default="")
            aid = safe_get(at, "teamId", default="")
            result += f"Game ID: {safe_get(game, 'gameId', default='N/A')}\n"
            result += f"{safe_get(at, 'teamName', default='Away Team')} ({safe_get(at, 'score', default=0)}) @ {safe_get(ht, 'teamName', default='Home Team')} ({safe_get(ht, 'score', default=0)})\n"
            result += f"Status: {safe_get(game, 'gameStatusText', default='Unknown')}\n"
            if hid not in ("", "N/A"):
                result += f"Home Team ID: {hid} | Logo: {get_team_logo_url(hid)}\n"
            if aid not in ("", "N/A"):
                result += f"Away Team ID: {aid} | Logo: {get_team_logo_url(aid)}\n"
            result += "\n"
        return result
    return await _tool_impl("get_todays_scoreboard", {}, _impl)


@mcp.tool(description="Games for a specific date (YYYYMMDD).")
async def get_scoreboard_by_date(date: str) -> list[TextContent]:
    _args: dict[str, Any] = {"date": date}

    async def _impl():
        date_str = date.strip()
        try:
            date_obj = datetime.strptime(date_str, "%Y%m%d")
            formatted_date = date_obj.strftime("%Y-%m-%d")
        except ValueError:
            return "Invalid date format. Use YYYYMMDD (e.g., '20241103')"
        url = f"{NBA_LIVE_API}/scoreboard/scoreboard_{date_str}.json"
        data = await fetch_nba_data(url)
        if not data:
            stats_games = await _get_scoreboard_games_stats_api(date_obj)
            if stats_games is None:
                return f"No data available for {formatted_date}. The game data might not be available yet or the date might be incorrect."
            if not stats_games:
                return f"No games found for {formatted_date}."
            result = f"NBA Games for {formatted_date}:\n\n"
            for g in stats_games:
                result += f"Game ID: {g.get('game_id', 'N/A')}\n{g.get('away_name', 'Away')} ({g.get('away_score', 'N/A')}) @ {g.get('home_name', 'Home')} ({g.get('home_score', 'N/A')})\nStatus: {g.get('status', 'Unknown')}\n\n"
            return result
        scoreboard = safe_get(data, "scoreboard")
        games = safe_get(scoreboard, "games", default=[])
        if not games:
            return f"No games found for {formatted_date}."
        result = f"NBA Games for {formatted_date}:\n\n"
        for game in games:
            ht = safe_get(game, "homeTeam", default={})
            at = safe_get(game, "awayTeam", default={})
            hid = safe_get(ht, "teamId", default="")
            aid = safe_get(at, "teamId", default="")
            result += f"Game ID: {safe_get(game, 'gameId', default='N/A')}\n"
            result += f"{safe_get(at, 'teamName')} ({safe_get(at, 'score')}) @ {safe_get(ht, 'teamName')} ({safe_get(ht, 'score')})\nStatus: {safe_get(game, 'gameStatusText', default='Unknown')}\n"
            if hid not in ("", "N/A"):
                result += f"Home Team ID: {hid} | Logo: {get_team_logo_url(hid)}\n"
            if aid not in ("", "N/A"):
                result += f"Away Team ID: {aid} | Logo: {get_team_logo_url(aid)}\n"
            result += "\n"
        return result
    return await _tool_impl("get_scoreboard_by_date", _args, _impl)


@mcp.tool(description="Detailed game info for a specific game_id.")
async def get_game_details(game_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            return "Please provide game_id."
        url = f"{NBA_LIVE_API}/scoreboard/todaysScoreboard_00.json"
        data = await fetch_nba_data(url)
        if data:
            games = safe_get(data, "scoreboard", "games", default=[])
            game = next((g for g in games if safe_get(g, "gameId") == gid), None)
            if game:
                ht = safe_get(game, "homeTeam", default={})
                at = safe_get(game, "awayTeam", default={})
                hid = safe_get(ht, "teamId", default="")
                aid = safe_get(at, "teamId", default="")
                result = f"Game Details for {gid}:\n\n"
                result += f"{safe_get(at, 'teamName')} @ {safe_get(ht, 'teamName')}\n"
                result += f"Score: {safe_get(at, 'score')} - {safe_get(ht, 'score')}\n"
                result += f"Status: {safe_get(game, 'gameStatusText')}\nPeriod: Q{safe_get(game, 'period', default=0)}\n"
                if hid not in ("", "N/A"):
                    result += f"Home Team ID: {hid} | Logo: {get_team_logo_url(hid)}\n"
                if aid not in ("", "N/A"):
                    result += f"Away Team ID: {aid} | Logo: {get_team_logo_url(aid)}\n"
                result += "\n"
                away_stats = safe_get(at, "statistics", default={})
                home_stats = safe_get(ht, "statistics", default={})
                if away_stats != "N/A" and home_stats != "N/A":
                    result += "Team Statistics:\n"
                    result += f"{safe_get(at, 'teamName')}:\n  FG: {safe_get(away_stats, 'fieldGoalsMade')}/{safe_get(away_stats, 'fieldGoalsAttempted')}\n  3P: {safe_get(away_stats, 'threePointersMade')}/{safe_get(away_stats, 'threePointersAttempted')}\n  FT: {safe_get(away_stats, 'freeThrowsMade')}/{safe_get(away_stats, 'freeThrowsAttempted')}\n  Rebounds: {safe_get(away_stats, 'reboundsTotal')}\n  Assists: {safe_get(away_stats, 'assists')}\n\n"
                    result += f"{safe_get(ht, 'teamName')}:\n  FG: {safe_get(home_stats, 'fieldGoalsMade')}/{safe_get(home_stats, 'fieldGoalsAttempted')}\n  3P: {safe_get(home_stats, 'threePointersMade')}/{safe_get(home_stats, 'threePointersAttempted')}\n  FT: {safe_get(home_stats, 'freeThrowsMade')}/{safe_get(home_stats, 'freeThrowsAttempted')}\n  Rebounds: {safe_get(home_stats, 'reboundsTotal')}\n  Assists: {safe_get(home_stats, 'assists')}\n"
                return result
        return f"Game {gid} not found in today's games. Try get_scoreboard_by_date to find the correct game_id."
    return await _tool_impl("get_game_details", _args, _impl)


@mcp.tool(description="Full box score for a game_id.")
async def get_box_score(game_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            return "Please provide game_id."
        url = f"{NBA_LIVE_API}/boxscore/boxscore_{gid}.json"
        live_data = await fetch_nba_data(url)
        if live_data and safe_get(live_data, "game") != "N/A":
            game = safe_get(live_data, "game", default={})
            ht = safe_get(game, "homeTeam", default={})
            at = safe_get(game, "awayTeam", default={})
            result = f"Box Score for Game {gid}:\n{safe_get(at, 'teamName')} @ {safe_get(ht, 'teamName')}\nFinal Score: {safe_get(at, 'score')} - {safe_get(ht, 'score')}\n\nTEAM STATS:\n"
            for label, tm in [("away", at), ("home", ht)]:
                stats = safe_get(tm, "statistics", default={})
                if stats != "N/A":
                    result += f"\n{safe_get(tm, 'teamName')}:\n  FG: {safe_get(stats, 'fieldGoalsMade')}/{safe_get(stats, 'fieldGoalsAttempted')}"
                    fg_pct = safe_get(stats, "fieldGoalsPercentage", default=0)
                    if fg_pct != "N/A":
                        result += f" ({format_stat(fg_pct, True)})"
                    result += "\n"
            result += "\n" + "=" * 70 + "\nPLAYER STATS:\n\n"
            for label, tm in [("away", at), ("home", ht)]:
                players = safe_get(tm, "players", default=[])
                if players and players != "N/A":
                    result += f"{safe_get(tm, 'teamName')}:\n{'Player':<25} {'MIN':<6} {'PTS':<5} {'REB':<5} {'AST':<5}\n" + "-" * 55 + "\n"
                    for player in players:
                        stats = safe_get(player, "statistics", default={})
                        if stats == "N/A":
                            continue
                        minutes = safe_get(stats, "minutes", default="0:00")
                        if not minutes or minutes == "0:00":
                            continue
                        result += f"{safe_get(player, 'name', default='Unknown'):<25} {minutes:<6} {safe_get(stats, 'points', default=0):<5} {safe_get(stats, 'reboundsTotal', default=0):<5} {safe_get(stats, 'assists', default=0):<5}\n"
                    result += "\n"
            return result
        # Stats API fallback
        url2 = f"{NBA_STATS_API}/boxscoretraditionalv2"
        params = {"GameID": gid, "StartPeriod": "0", "EndPeriod": "10", "RangeType": "0", "StartRange": "0", "EndRange": "0"}
        data = await fetch_nba_data(url2, params)
        if not data:
            return "Error fetching box score. The game stats may not be available yet."
        player_stats_rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        team_stats_rows = safe_get(data, "resultSets", 1, "rowSet", default=[])
        if not player_stats_rows or player_stats_rows == "N/A":
            return f"Box score not available for game {gid}."
        result = f"Box Score for Game {gid} (Stats API):\n\n"
        if team_stats_rows and team_stats_rows != "N/A":
            result += "TEAM STATS:\n"
            for t in team_stats_rows:
                result += f"  {safe_get(t, 1, default='N/A')}: {safe_get(t, 24, default=0)} PTS\n"
        result += f"\nPLAYER STATS: (summary)\nPlayers returned: {len(player_stats_rows)}\n"
        return result
    return await _tool_impl("get_box_score", _args, _impl)


@mcp.tool(description="Search players by name substring.")
async def search_players(query: str) -> list[TextContent]:
    _args: dict[str, Any] = {"query": query}

    async def _impl():
        q = query.strip().lower()
        if not q:
            return "Please provide a non-empty query."
        url = f"{NBA_STATS_API}/commonallplayers"
        params = {"LeagueID": "00", "Season": get_current_season(), "IsOnlyCurrentSeason": "0"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching player data. Please try again."
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not rows:
            return "No players found."
        matching = []
        for row in rows:
            if len(row) > 2:
                name_ = str(row[2]).lower()
                if q in name_:
                    matching.append({"id": row[0], "name": row[2], "is_active": row[11] if len(row) > 11 else 1})
        if not matching:
            return f"No players found matching '{query}'."
        result = f"Found {len(matching)} player(s):\n\n"
        for p in matching[:20]:
            status = "Active" if p["is_active"] == 1 else "Inactive"
            pid = p["id"]
            result += f"ID: {pid} | Name: {p['name']} | Status: {status} | Headshot: {get_player_headshot_url(pid)} | Thumb: {get_player_headshot_thumbnail_url(pid)}\n"
        if len(matching) > 20:
            result += f"\n... and {len(matching) - 20} more."
        return result
    return await _tool_impl("search_players", _args, _impl)


@mcp.tool(description="Player bio/profile info.")
async def get_player_info(player_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"player_id": player_id}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        url = f"{NBA_STATS_API}/commonplayerinfo"
        params = {"PlayerID": pid}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching player info. Please try again."
        info_headers = safe_get(data, "resultSets", 0, "headers", default=[])
        player_data = safe_get(data, "resultSets", 0, "rowSet", 0, default=[])
        if not player_data or player_data == "N/A":
            return "Player not found."
        result = "Player Information:\n\n"
        result += f"Player ID: {pid}\nHeadshot (1040x760): {get_player_headshot_url(pid)}\nHeadshot (260x190): {get_player_headshot_thumbnail_url(pid)}\n"
        result += f"Name: {safe_get(player_data, 3)}\n"

        def _hidx(col, fallback=None):
            try:
                return info_headers.index(col)
            except Exception:
                return fallback

        for col, label in [("JERSEY", "Jersey: #"), ("POSITION", "Position: "), ("ROSTERSTATUS", "Status: "), ("HEIGHT", "Height: "), ("WEIGHT", "Weight: "), ("BIRTHDATE", "Birth Date: "), ("COUNTRY", "Country: "), ("SCHOOL", "School: ")]:
            idx = _hidx(col)
            if idx is not None:
                val = safe_get(player_data, idx)
                suffix = " lbs" if col == "WEIGHT" else ""
                result += f"{label}{val}{suffix}\n"

        ti_idx = _hidx("TEAM_ID")
        tn_idx = _hidx("TEAM_NAME")
        ta_idx = _hidx("TEAM_ABBREVIATION")
        tiv = safe_get(player_data, ti_idx, default="") if ti_idx is not None else ""
        tnv = safe_get(player_data, tn_idx, default="") if tn_idx is not None else ""
        tav = safe_get(player_data, ta_idx, default="") if ta_idx is not None else ""
        if tiv:
            result += f"Team: {tnv} (ID: {tiv})"
            if tav and tav != "N/A":
                result += f" [{tav}]"
            result += f" | Logo: {get_team_logo_url(tiv)}\n"
        elif tnv:
            result += f"Team: {tnv}\n"
        return result
    return await _tool_impl("get_player_info", _args, _impl)


@mcp.tool(description="Player stats for a season.")
async def get_player_season_stats(player_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player_id": player_id, "season": season}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        url = f"{NBA_STATS_API}/playercareerstats"
        params = {"PlayerID": pid, "PerMode": "PerGame"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching player stats. Please try again."
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        all_seasons = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not all_seasons:
            return "No stats found for this player."
        season_id_idx = headers.index("SEASON_ID") if "SEASON_ID" in headers else 1
        stats_row = next((r for r in all_seasons if str(safe_get(r, season_id_idx)) == season), None)
        if not stats_row:
            return f"No stats found for season {season}."

        def _idx(col, fb):
            try:
                return headers.index(col)
            except ValueError:
                return fb
        result = f"Season Stats ({season}) - Player ID {pid}:\n\nHeadshot: {get_player_headshot_url(pid)}\n"
        result += f"GP: {safe_get(stats_row, _idx('GP', 6))} | MIN: {format_stat(safe_get(stats_row, _idx('MIN', 8)))}\n"
        result += f"PTS: {format_stat(safe_get(stats_row, _idx('PTS', 26)))} | REB: {format_stat(safe_get(stats_row, _idx('REB', 18)))} | AST: {format_stat(safe_get(stats_row, _idx('AST', 19)))}\n"
        result += f"STL: {format_stat(safe_get(stats_row, _idx('STL', 21)))} | BLK: {format_stat(safe_get(stats_row, _idx('BLK', 22)))}\n"
        result += f"FG%: {format_stat(safe_get(stats_row, _idx('FG_PCT', 9)), True)} | 3P%: {format_stat(safe_get(stats_row, _idx('FG3_PCT', 12)), True)} | FT%: {format_stat(safe_get(stats_row, _idx('FT_PCT', 15)), True)}\n"
        return result
    return await _tool_impl("get_player_season_stats", _args, _impl)


@mcp.tool(description="Player game log for a season.")
async def get_player_game_log(player_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player_id": player_id, "season": season}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        url = f"{NBA_STATS_API}/playergamelog"
        params = {"PlayerID": pid, "Season": season, "SeasonType": "Regular Season"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching game log. Please try again."
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        games = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not games:
            return f"No games found for season {season}."

        def _idx(col, fb):
            try:
                return headers.index(col)
            except ValueError:
                return fb
        result = f"Game Log - Player {pid} ({season}):\n\n"
        for g in games[:10]:
            result += f"{safe_get(g, _idx('GAME_DATE', 2))} | {safe_get(g, _idx('MATCHUP', 3))} | {safe_get(g, _idx('WL', 4))} | "
            result += f"{safe_get(g, _idx('PTS', 24))} PTS, {safe_get(g, _idx('REB', 18))} REB, {safe_get(g, _idx('AST', 19))} AST | MIN {safe_get(g, _idx('MIN', 5))}\n"
        if len(games) > 10:
            result += f"\n... {len(games) - 10} more games."
        return result
    return await _tool_impl("get_player_game_log", _args, _impl)


@mcp.tool(description="Player career totals/averages.")
async def get_player_career_stats(player_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"player_id": player_id}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        url = f"{NBA_STATS_API}/playercareerstats"
        params = {"PlayerID": pid, "PerMode": "Totals"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching career stats. Please try again."
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not rows or rows == "N/A":
            return "No career stats found for this player."
        tg = tp = tr = ta = ts = tb = tm = 0.0
        for sr in rows:
            if len(sr) > 26:
                tg += float(sr[6]) if sr[6] else 0
                tm += float(sr[8]) if sr[8] else 0
                tr += float(sr[20]) if sr[20] else 0
                ta += float(sr[21]) if sr[21] else 0
                ts += float(sr[22]) if sr[22] else 0
                tb += float(sr[23]) if sr[23] else 0
                tp += float(sr[26]) if sr[26] else 0
        ppg = tp / tg if tg > 0 else 0
        rpg = tr / tg if tg > 0 else 0
        apg = ta / tg if tg > 0 else 0
        result = f"Career Statistics (Regular Season):\n\nPlayer ID: {pid}\nHeadshot: {get_player_headshot_url(pid)}\n\n"
        result += f"Total Points: {int(tp):,}\nGames Played: {int(tg):,}\nTotal Rebounds: {int(tr):,}\nTotal Assists: {int(ta):,}\nTotal Steals: {int(ts):,}\nTotal Blocks: {int(tb):,}\nTotal Minutes: {int(tm):,}\n\n"
        result += f"Career Averages:\nPPG: {ppg:.1f} | RPG: {rpg:.1f} | APG: {apg:.1f}\n"
        return result
    return await _tool_impl("get_player_career_stats", _args, _impl)


@mcp.tool(description="Player hustle stats.")
async def get_player_hustle_stats(player_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player_id": player_id, "season": season}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        url = f"{NBA_STATS_API}/leaguehustlestatsplayer"
        params = {"Season": season, "SeasonType": "Regular Season", "PerMode": "Totals"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching hustle stats. Please try again."
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        ps = next((r for r in rows if str(safe_get(r, 0)) == str(pid)), None)
        if not ps:
            return f"No hustle stats found for player ID {pid} in season {season}."
        result = f"Hustle Statistics - {safe_get(ps, 1, default='Player')} ({safe_get(ps, 3, default='N/A')}) [{season}]:\n\nGames Played: {safe_get(ps, 5, default=0)}\n\n"
        result += f"Deflections: {safe_get(ps, 10, default=0)}\nCharges Drawn: {safe_get(ps, 11, default=0)}\nScreen Assists: {safe_get(ps, 12, default=0)}\nLoose Balls Recovered: {safe_get(ps, 16, default=0)}\nBox Outs: {safe_get(ps, 23, default=0)}\n"
        return result
    return await _tool_impl("get_player_hustle_stats", _args, _impl)


@mcp.tool(description="League leaders in a hustle stat category.")
async def get_league_hustle_leaders(stat_category: str = "deflections", season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"stat_category": stat_category, "season": season}

    async def _impl():
        url = f"{NBA_STATS_API}/leaguehustlestatsplayer"
        params = {"Season": season, "SeasonType": "Regular Season", "PerMode": "Totals"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching hustle stats. Please try again."
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        sm = {"deflections": (10, "Deflections"), "charges": (11, "Charges Drawn"), "screen_assists": (12, "Screen Assists"), "loose_balls": (16, "Loose Balls Recovered"), "box_outs": (23, "Box Outs")}
        if stat_category not in sm:
            return f"Invalid stat category. Choose from: {', '.join(sm.keys())}"
        ci, sn = sm[stat_category]
        sp = sorted(rows, key=lambda x: float(safe_get(x, ci, default=0) or 0), reverse=True)[:10]
        result = f"League Hustle Leaders - {sn} ({season}):\n\n"
        for i, p in enumerate(sp, 1):
            result += f"{i}. {safe_get(p, 1, default='Unknown')} ({safe_get(p, 3, default='N/A')}): {safe_get(p, ci, default=0)}\n"
        return result
    return await _tool_impl("get_league_hustle_leaders", _args, _impl)


@mcp.tool(description="Opponent FG% when defended by player.")
async def get_player_defense_stats(player_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player_id": player_id, "season": season}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        url = f"{NBA_STATS_API}/leaguedashptdefend"
        params = {"Season": season, "SeasonType": "Regular Season", "PerMode": "Totals", "DefenseCategory": "Overall"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching defense stats. Please try again."
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        ps = next((r for r in rows if str(safe_get(r, 0)) == str(pid)), None)
        if not ps:
            return f"No defense stats found for player ID {pid} in season {season}."
        result = f"Defensive Impact - {safe_get(ps, 1, default='Player')} ({safe_get(ps, 3, default='N/A')}) [{season}]:\n\n"
        result += f"Position: {safe_get(ps, 4, default='N/A')}\nGames: {safe_get(ps, 6, default=0)}\n\n"
        result += f"Opponent FG% when defended: {format_stat(safe_get(ps, 11, default=0), True)}\nOpponent normal FG%: {format_stat(safe_get(ps, 12, default=0), True)}\nDifference: {format_stat(safe_get(ps, 13, default=0), True)}\n"
        return result
    return await _tool_impl("get_player_defense_stats", _args, _impl)


@mcp.tool(description="All-time leaders for a stat category.")
async def get_all_time_leaders(stat_category: str = "points", limit: int = 10) -> list[TextContent]:
    _args: dict[str, Any] = {"stat_category": stat_category, "limit": limit}

    async def _impl():
        sc = stat_category.lower()
        lim = min(limit, 50)
        sm = {"points": "PTSLeaders", "rebounds": "REBLeaders", "assists": "ASTLeaders", "steals": "STLLeaders", "blocks": "BLKLeaders", "games": "GPLeaders", "offensive_rebounds": "OREBLeaders", "defensive_rebounds": "DREBLeaders", "field_goals_made": "FGMLeaders", "field_goals_attempted": "FGALeaders", "field_goal_pct": "FG_PCTLeaders", "three_pointers_made": "FG3MLeaders", "three_pointers_attempted": "FG3ALeaders", "three_point_pct": "FG3_PCTLeaders", "free_throws_made": "FTMLeaders", "free_throws_attempted": "FTALeaders", "free_throw_pct": "FT_PCTLeaders", "turnovers": "TOVLeaders", "personal_fouls": "PFLeaders"}
        if sc not in sm:
            return f"Invalid stat category. Choose from: {', '.join(sorted(sm.keys()))}"
        url = f"{NBA_STATS_API}/alltimeleadersgrids"
        params = {"LeagueID": "00", "PerMode": "Totals", "SeasonType": "Regular Season", "TopX": str(lim)}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching all-time leaders. Please try again."
        leaders_data = None
        for rs in safe_get(data, "resultSets", default=[]):
            if rs.get("name") == sm[sc]:
                leaders_data = rs.get("rowSet", [])
                break
        if not leaders_data:
            return f"No all-time leaders found for {sc}."
        result = f"All-Time Career Leaders - {sc.replace('_', ' ').title()}:\n\n"
        for i, p in enumerate(leaders_data, 1):
            val = safe_get(p, 2, default=0)
            if "pct" in sc:
                vs = format_stat(val, is_percentage=True)
            else:
                try:
                    vs = f"{int(float(val)):,}"
                except (ValueError, TypeError):
                    vs = str(val)
            active = " ✓" if safe_get(p, 4, default=0) == 1 else ""
            result += f"{i}. {safe_get(p, 1, default='Unknown')}: {vs}{active}\n"
        if any(safe_get(p, 4, default=0) == 1 for p in leaders_data):
            result += "\n✓ = Active player"
        return result
    return await _tool_impl("get_all_time_leaders", _args, _impl)


@mcp.tool(description="Team roster.")
async def get_team_roster(team_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"team_id": team_id, "season": season}

    async def _impl():
        tid = team_id.strip()
        if not tid:
            return "Please provide team_id."
        url = f"{NBA_STATS_API}/commonteamroster"
        params = {"TeamID": tid, "Season": season}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching roster. Please try again."
        roster_data = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not roster_data:
            return "No roster found for this team."
        result = f"Team Roster ({season}) - Team ID {tid} | Logo: {get_team_logo_url(tid)}\n\n"
        for p in roster_data:
            pn = safe_get(p, 3)
            num = safe_get(p, 4)
            pos = safe_get(p, 5)
            pid = safe_get(p, 14, default="")
            result += f"#{num} {pn} - {pos}"
            if pid not in ("", "N/A"):
                result += f" | Player ID: {pid} | Headshot: {get_player_headshot_url(pid)}"
            result += "\n"
        return result
    return await _tool_impl("get_team_roster", _args, _impl)


@mcp.tool(description="League standings.")
async def get_standings(season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"season": season}

    async def _impl():
        url = f"{NBA_STATS_API}/leaguestandingsv3"
        params = {"LeagueID": "00", "Season": season, "SeasonType": "Regular Season"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching standings. Please try again."
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not rows:
            return "No standings found."

        def _idx(col, fb=None):
            try:
                return headers.index(col)
            except Exception:
                return fb

        tni = _idx("TeamName", 4)
        ci = _idx("Conference", 5)
        wi = _idx("WINS", 13)
        li = _idx("LOSSES", 14)
        pi = _idx("WinPCT", 15)
        tii = _idx("TeamID", _idx("TEAM_ID"))
        east = [r for r in rows if safe_get(r, ci, default="") == "East"]
        west = [r for r in rows if safe_get(r, ci, default="") != "East"]

        def _pct(row):
            try:
                return float(safe_get(row, pi, default=0) or 0)
            except Exception:
                return 0.0
        east.sort(key=_pct, reverse=True)
        west.sort(key=_pct, reverse=True)
        result = f"NBA Standings ({season}):\n\nEastern Conference:\n"
        for i, r in enumerate(east, 1):
            tid = safe_get(r, tii, default="") if tii is not None else ""
            result += f"{i}. {safe_get(r, tni)}: {safe_get(r, wi)}-{safe_get(r, li)} ({format_stat(safe_get(r, pi))})"
            if tid not in ("", "N/A"):
                result += f" | Team ID: {tid} | Logo: {get_team_logo_url(tid)}"
            result += "\n"
        result += "\nWestern Conference:\n"
        for i, r in enumerate(west, 1):
            tid = safe_get(r, tii, default="") if tii is not None else ""
            result += f"{i}. {safe_get(r, tni)}: {safe_get(r, wi)}-{safe_get(r, li)} ({format_stat(safe_get(r, pi))})"
            if tid not in ("", "N/A"):
                result += f" | Team ID: {tid} | Logo: {get_team_logo_url(tid)}"
            result += "\n"
        return result
    return await _tool_impl("get_standings", _args, _impl)


@mcp.tool(description="Current season per-game league leaders for a stat category (Points/Assists/Rebounds/etc.).")
async def get_league_leaders(stat_type: str = "Points", season: str = "", limit: int = 10) -> list[TextContent]:
    season = season or get_current_season()
    limit = min(limit, 50)
    _args: dict[str, Any] = {"stat_type": stat_type, "season": season, "limit": limit}

    async def _impl():
        sm = {"Points": "PTS", "Assists": "AST", "Rebounds": "REB", "Steals": "STL", "Blocks": "BLK", "FG%": "FG_PCT", "3P%": "FG3_PCT", "FT%": "FT_PCT"}
        stat_category = sm.get(stat_type, sm.get(stat_type.title(), "PTS"))
        url = f"{NBA_STATS_API}/leaguedashplayerstats"
        params = {"LeagueID": "00", "Season": season, "SeasonType": "Regular Season", "PerMode": "PerGame", "MeasureType": "Base", "PlusMinus": "N", "PaceAdjust": "N", "Rank": "N", "Outcome": "", "Location": "", "Month": "0", "SeasonSegment": "", "DateFrom": "", "DateTo": "", "OpponentTeamID": "0", "VsConference": "", "VsDivision": "", "GameSegment": "", "Period": "0", "LastNGames": "0"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching league leaders. Please try again."
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not rows or not headers:
            return f"No data found for {stat_type} leaders."
        try:
            pii = headers.index("PLAYER_ID")
        except ValueError:
            pii = 0
        try:
            pni = headers.index("PLAYER_NAME")
        except ValueError:
            pni = 1
        try:
            tai = headers.index("TEAM_ABBREVIATION")
        except ValueError:
            tai = 3
        if stat_category not in headers:
            return f"Unsupported stat_type '{stat_type}'. Try one of: {', '.join(sorted(sm.keys()))}"
        si = headers.index(stat_category)

        def _val(row):
            try:
                return float(safe_get(row, si, default=0))
            except Exception:
                return 0.0
        sorted_rows = sorted(rows, key=_val, reverse=True)
        result = f"League Leaders - {stat_type} ({season}) [Per Game]:\n\n"
        for i, row in enumerate(sorted_rows[:limit], 1):
            val = safe_get(row, si, default="N/A")
            if stat_category.endswith("_PCT"):
                try:
                    val_s = f"{float(val) * 100:.1f}%"
                except Exception:
                    val_s = str(val)
            else:
                val_s = str(val)
            result += f"{i}. {safe_get(row, pni, default='Unknown')} ({safe_get(row, tai, default='N/A')}) - {val_s} | Player ID: {safe_get(row, pii, default='N/A')}\n"
        return result
    return await _tool_impl("get_league_leaders", _args, _impl)


@mcp.tool(description="Team upcoming schedule.")
async def get_schedule(team_id: str, days_ahead: int = 7) -> list[TextContent]:
    days_ahead = min(days_ahead, 90)
    _args: dict[str, Any] = {"team_id": team_id, "days_ahead": days_ahead}

    async def _impl():
        tid = team_id.strip()
        if not tid:
            return "Please specify team_id."
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        data = await fetch_nba_data(url)
        if not data:
            return "Error fetching schedule. Please try again."
        game_dates = safe_get(data, "leagueSchedule", "gameDates", default=[])
        if not game_dates:
            return "No schedule data available."
        today = datetime.now()
        tid_int = int(tid)
        upcoming = []
        for de in game_dates:
            for game in safe_get(de, "games", default=[]):
                hid = safe_get(game, "homeTeam", "teamId")
                aid = safe_get(game, "awayTeam", "teamId")
                if hid == tid_int or aid == tid_int:
                    gds = safe_get(game, "gameDateTimeEst")
                    if gds == "N/A":
                        continue
                    try:
                        gd = datetime.fromisoformat(str(gds).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if gd.date() >= today.date() and (gd.date() - today.date()).days <= days_ahead:
                        upcoming.append((gd, game))
        upcoming.sort(key=lambda x: x[0])
        if not upcoming:
            return f"No upcoming games found within the next {days_ahead} days for this team."
        tn = NBA_TEAMS.get(tid_int, f"Team {tid}")
        result = f"Upcoming Games for {tn} (Team ID {tid}):\n(Next {days_ahead} days) | Logo: {get_team_logo_url(tid)}\n\n"
        for gd, game in upcoming:
            ht = safe_get(game, "homeTeam", default={})
            at = safe_get(game, "awayTeam", default={})
            result += f"{gd.strftime('%Y-%m-%d')} - {safe_get(at, 'teamCity')} {safe_get(at, 'teamName')} @ {safe_get(ht, 'teamCity')} {safe_get(ht, 'teamName')}\n"
            result += f"  Arena: {safe_get(game, 'arenaName')} | Game ID: {safe_get(game, 'gameId')}\n\n"
        return result
    return await _tool_impl("get_schedule", _args, _impl)


@mcp.tool(description="Player awards/accolades.")
async def get_player_awards(player_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"player_id": player_id}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        url = f"{NBA_STATS_API}/playerawards"
        params = {"PlayerID": pid}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching player awards. Please try again."
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        awards = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not awards:
            return "No awards found for this player."

        def _idx(col, fb):
            try:
                return headers.index(col)
            except Exception:
                return fb
        di = _idx("DESCRIPTION", 4)
        si = _idx("SEASON", 6)
        ti = _idx("TEAM", 3)
        first = awards[0]
        pn = f"{safe_get(first, 1)} {safe_get(first, 2)}".strip()
        result = f"Awards and Accolades - {pn} (Player ID {pid})\n\nHeadshot: {get_player_headshot_url(pid)}\n\n"
        for a in awards[:50]:
            result += f"{safe_get(a, si)}: {safe_get(a, di)}"
            t = safe_get(a, ti, default="")
            if t and t != "N/A":
                result += f" ({t})"
            result += "\n"
        if len(awards) > 50:
            result += f"\n... and {len(awards) - 50} more."
        return result
    return await _tool_impl("get_player_awards", _args, _impl)


@mcp.tool(description="Major awards for a season.")
async def get_season_awards(season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"season": season}

    async def _impl():
        mvp_map = {
            "2023-24": ("Joel Embiid", "1610612755"), "2022-23": ("Joel Embiid", "1610612755"),
            "2021-22": ("Nikola Jokic", "1610612743"), "2020-21": ("Nikola Jokic", "1610612743"),
            "2019-20": ("Giannis Antetokounmpo", "1610612749"), "2018-19": ("Giannis Antetokounmpo", "1610612749"),
            "2017-18": ("James Harden", "1610612745"), "2016-17": ("Russell Westbrook", "1610612760"),
            "2015-16": ("Stephen Curry", "1610612744"), "2014-15": ("Stephen Curry", "1610612744"),
        }
        if season not in mvp_map:
            return f"Award data for {season} season is not available. Use get_player_awards for individual awards."
        mvp_name, tid = mvp_map[season]
        return f"Major Awards - {season} Season\n\nMVP: {mvp_name} | Team ID: {tid} | Logo: {get_team_logo_url(tid)}\n"
    return await _tool_impl("get_season_awards", _args, _impl)


@mcp.tool(description="Shot chart data summary.")
async def get_shot_chart(player_id: str, season: str = "", game_id: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player_id": player_id, "season": season, "game_id": game_id}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        params = {"PlayerID": pid, "Season": season, "SeasonType": "Regular Season", "TeamID": "0", "GameID": game_id.strip(), "Outcome": "", "Location": "", "Month": "0", "SeasonSegment": "", "DateFrom": "", "DateTo": "", "OpponentTeamID": "0", "VsConference": "", "VsDivision": "", "Position": "", "RookieYear": "", "GameSegment": "", "Period": "0", "LastNGames": "0", "ContextMeasure": "FGA"}
        url = f"{NBA_STATS_API}/shotchartdetail"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Failed to fetch shot chart data."
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        shots = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not shots:
            return f"No shot data found for this player in {season}."
        try:
            mi = headers.index("SHOT_MADE_FLAG")
            di = headers.index("SHOT_DISTANCE")
        except ValueError:
            return "Error parsing shot chart data structure."
        total = len(shots)
        made = sum(1 for s in shots if safe_get(s, mi) == 1)
        pct = (made / total * 100) if total > 0 else 0.0
        try:
            avg_dist = sum(float(safe_get(s, di, default=0) or 0) for s in shots) / total
        except Exception:
            avg_dist = 0.0
        result = f"Shot Chart Summary - Player {pid} ({season})\n\nHeadshot: {get_player_headshot_url(pid)}\n"
        result += f"Shots: {made}/{total} ({pct:.1f}%) | Avg Distance: {avg_dist:.1f} ft\n"
        result += "Note: For visualization, use the raw coordinates from the shotchartdetail endpoint."
        return result
    return await _tool_impl("get_shot_chart", _args, _impl)


@mcp.tool(description="Shooting splits summary.")
async def get_shooting_splits(player_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player_id": player_id, "season": season}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        params = {"PlayerID": pid, "Season": season, "SeasonType": "Regular Season", "PerMode": "Totals", "MeasureType": "Base", "PlusMinus": "N", "PaceAdjust": "N", "Rank": "N", "Outcome": "", "Location": "", "Month": "0", "SeasonSegment": "", "DateFrom": "", "DateTo": "", "OpponentTeamID": "0", "VsConference": "", "VsDivision": "", "GameSegment": "", "Period": "0", "LastNGames": "0"}
        url = f"{NBA_STATS_API}/playerdashboardbyshootingsplits"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Failed to fetch shooting splits data."
        result_sets = safe_get(data, "resultSets", default=[])
        overall = next((rs for rs in result_sets if safe_get(rs, "name") == "OverallPlayerDashboard"), None)
        if not overall:
            return f"No shooting data found for {season}."
        headers = safe_get(overall, "headers", default=[])
        rows = safe_get(overall, "rowSet", default=[])
        if not rows:
            return f"No shooting data available for {season}."
        row = rows[0]
        pn = safe_get(row, 1, default="Player")
        try:
            fgmi = headers.index("FGM")
            fgai = headers.index("FGA")
            fgpi = headers.index("FG_PCT")
            fg3mi = headers.index("FG3M")
            fg3ai = headers.index("FG3A")
            fg3pi = headers.index("FG3_PCT")
        except ValueError:
            return "Unable to parse shooting splits."
        result = f"Shooting Splits - {pn} ({season})\n\nPlayer ID: {pid} | Headshot: {get_player_headshot_url(pid)}\n"
        result += f"FG: {safe_get(row, fgmi, default=0)}/{safe_get(row, fgai, default=0)} ({format_stat(safe_get(row, fgpi, default=0), True)})\n"
        result += f"3P: {safe_get(row, fg3mi, default=0)}/{safe_get(row, fg3ai, default=0)} ({format_stat(safe_get(row, fg3pi, default=0), True)})\n"
        return result
    return await _tool_impl("get_shooting_splits", _args, _impl)


@mcp.tool(description="Play-by-play summary.")
async def get_play_by_play(game_id: str, start_period: int = 1, end_period: int = 10) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id, "start_period": start_period, "end_period": end_period}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            return "Please provide game_id."
        params = {"GameID": gid, "StartPeriod": start_period, "EndPeriod": end_period}
        url = f"{NBA_STATS_API}/playbyplayv2"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Failed to fetch play-by-play data."
        result_sets = safe_get(data, "resultSets", default=[])
        pbp = next((rs for rs in result_sets if safe_get(rs, "name") == "PlayByPlay"), None)
        if not pbp:
            return f"No play-by-play data found for game {gid}."
        headers = safe_get(pbp, "headers", default=[])
        plays = safe_get(pbp, "rowSet", default=[])
        if not plays:
            return f"No plays found for game {gid}."

        def _idx(col, fb):
            try:
                return headers.index(col)
            except ValueError:
                return fb
        pi = _idx("PERIOD", 4)
        ti = _idx("PCTIMESTRING", 6)
        hdi = _idx("HOMEDESCRIPTION", 7)
        vdi = _idx("VISITORDESCRIPTION", 9)
        sci = _idx("SCORE", 10)
        result = f"Play-by-Play (sample) - Game {gid}\nShowing periods {start_period} to {end_period}\n\n"
        count = 0
        for play in plays:
            desc = safe_get(play, hdi, default="") or safe_get(play, vdi, default="")
            if not desc or desc == "N/A":
                continue
            count += 1
            result += f"Q{safe_get(play, pi, default='')} {safe_get(play, ti, default='')}: {desc}"
            s = safe_get(play, sci, default="")
            if s and s != "N/A":
                result += f" [{s}]"
            result += "\n"
            if count >= 25:
                break
        if len(plays) > count:
            result += f"\n... showing first {count} plays (out of {len(plays)})."
        return result
    return await _tool_impl("get_play_by_play", _args, _impl)


@mcp.tool(description="Rotation/substitution summary.")
async def get_game_rotation(game_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            return "Please provide game_id."
        params = {"GameID": gid, "LeagueID": "00"}
        url = f"{NBA_STATS_API}/gamerotation"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Failed to fetch game rotation data."
        result_sets = safe_get(data, "resultSets", default=[])
        away = next((rs for rs in result_sets if safe_get(rs, "name") == "AwayTeam"), None)
        home = next((rs for rs in result_sets if safe_get(rs, "name") == "HomeTeam"), None)
        if not away and not home:
            return f"No rotation data found for game {gid}."

        def _summ(team_data, label):
            headers = safe_get(team_data, "headers", default=[])
            rows = safe_get(team_data, "rowSet", default=[])
            if not rows:
                return ""
            try:
                fi = headers.index("PLAYER_FIRST")
                li = headers.index("PLAYER_LAST")
                pi = headers.index("PLAYER_PTS")
            except ValueError:
                return ""
            pts_by = {}
            for r in rows:
                n = f"{safe_get(r, fi, default='')} {safe_get(r, li, default='')}".strip()
                try:
                    pts_by[n] = max(pts_by.get(n, 0), int(float(safe_get(r, pi, default=0) or 0)))
                except Exception:
                    pts_by[n] = pts_by.get(n, 0)
            top = sorted(pts_by.items(), key=lambda x: x[1], reverse=True)[:10]
            out = f"{label} Rotation Summary (top points):\n"
            for n, p in top:
                out += f"  {n}: {p} pts\n"
            return out + "\n"

        result = f"Game Rotation Summary - Game {gid}\n\n"
        if away:
            result += _summ(away, "Away")
        if home:
            result += _summ(home, "Home")
        return result
    return await _tool_impl("get_game_rotation", _args, _impl)


@mcp.tool(description="Player advanced metrics summary.")
async def get_player_advanced_stats(player_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player_id": player_id, "season": season}

    async def _impl():
        pid = player_id.strip()
        if not pid:
            return "Please provide player_id."
        params = {"PlayerID": pid, "Season": season, "SeasonType": "Regular Season", "MeasureType": "Advanced", "PerMode": "PerGame", "PlusMinus": "N", "PaceAdjust": "N", "Rank": "N", "LastNGames": "0", "Month": "0", "OpponentTeamID": "0", "Period": "0", "DateFrom": "", "DateTo": "", "GameSegment": "", "LeagueID": "00", "Location": "", "Outcome": "", "PORound": "0", "SeasonSegment": "", "ShotClockRange": "", "VsConference": "", "VsDivision": ""}
        url = f"{NBA_STATS_API}/playerdashboardbygeneralsplits"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Failed to fetch player advanced stats."
        result_sets = safe_get(data, "resultSets", default=[])
        overall = next((rs for rs in result_sets if safe_get(rs, "name") == "OverallPlayerDashboard"), None)
        if not overall:
            return f"No advanced stats found for {season}."
        headers = safe_get(overall, "headers", default=[])
        rows = safe_get(overall, "rowSet", default=[])
        if not rows:
            return f"No advanced stats available for {season}."
        row = rows[0]

        def _idx(col):
            try:
                return headers.index(col)
            except ValueError:
                return None
        ni = _idx("PLAYER_NAME") or 1
        result = f"Advanced Stats - {safe_get(row, ni, default='Player')} ({season})\n\nPlayer ID: {pid} | Headshot: {get_player_headshot_url(pid)}\n"
        for col, label in [("OFF_RATING", "OffRtg"), ("DEF_RATING", "DefRtg"), ("NET_RATING", "NetRtg")]:
            idx = _idx(col)
            if idx is not None:
                result += f"{label}: {format_stat(safe_get(row, idx))}\n"
        for col, label in [("TS_PCT", "TS%"), ("USG_PCT", "USG%")]:
            idx = _idx(col)
            if idx is not None:
                result += f"{label}: {format_stat(safe_get(row, idx), True)}\n"
        return result
    return await _tool_impl("get_player_advanced_stats", _args, _impl)


@mcp.tool(description="Team advanced metrics summary.")
async def get_team_advanced_stats(team_id: str, season: str = "") -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"team_id": team_id, "season": season}

    async def _impl():
        tid = team_id.strip()
        if not tid:
            return "Please provide team_id."
        params = {"TeamID": tid, "Season": season, "SeasonType": "Regular Season", "MeasureType": "Advanced", "PerMode": "PerGame", "PlusMinus": "N", "PaceAdjust": "N", "Rank": "N", "LastNGames": "0", "Month": "0", "OpponentTeamID": "0", "Period": "0", "DateFrom": "", "DateTo": "", "GameSegment": "", "LeagueID": "00", "Location": "", "Outcome": "", "PORound": "0", "SeasonSegment": "", "ShotClockRange": "", "VsConference": "", "VsDivision": ""}
        url = f"{NBA_STATS_API}/teamdashboardbygeneralsplits"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Failed to fetch team advanced stats."
        result_sets = safe_get(data, "resultSets", default=[])
        overall = next((rs for rs in result_sets if safe_get(rs, "name") == "OverallTeamDashboard"), None)
        if not overall:
            return f"No advanced stats found for {season}."
        headers = safe_get(overall, "headers", default=[])
        rows = safe_get(overall, "rowSet", default=[])
        if not rows:
            return f"No advanced stats available for {season}."
        row = rows[0]

        def _idx(col):
            try:
                return headers.index(col)
            except ValueError:
                return None
        ni = _idx("TEAM_NAME") or _idx("GROUP_VALUE") or 1
        result = f"Advanced Stats - {safe_get(row, ni, default='Team')} ({season})\n\nTeam ID: {tid} | Logo: {get_team_logo_url(tid)}\n"
        for col, label in [("OFF_RATING", "OffRtg"), ("DEF_RATING", "DefRtg"), ("NET_RATING", "NetRtg"), ("PACE", "Pace")]:
            idx = _idx(col)
            if idx is not None:
                result += f"{label}: {format_stat(safe_get(row, idx))}\n"
        return result
    return await _tool_impl("get_team_advanced_stats", _args, _impl)


# ==================== Entrypoints ====================


async def async_main() -> None:
    """Run the server using stdio transport (for backward compatibility)."""
    await mcp.run_stdio_async()


def main() -> None:
    """Run the server with configurable transport."""
    import argparse
    parser = argparse.ArgumentParser(description="NBA MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
