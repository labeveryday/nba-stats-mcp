#!/usr/bin/env python3
"""
NBA MCP Server v0.3.0 — Name-First, Consolidated, Structured

22 tools (down from 30). Every tool accepts human names, returns structured
data + compact text, and defaults season to current.

Response shape (always JSON string in TextContent.text):
{
  "entity_type": "tool_result",
  "schema_version": "3.0",
  "tool_name": "...",
  "arguments": {...},
  "text": "...",            # 1-3 line compact summary
  "data": {...},            # structured machine-readable data
  "entities": {...},        # extracted ids + CDN asset URLs
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

# Configure logging
log_level = os.getenv("NBA_MCP_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.WARNING),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("nba-stats-mcp")

# NBA API endpoints
NBA_LIVE_API = "https://cdn.nba.com/static/json/liveData"
NBA_STATS_API = "https://stats.nba.com/stats"

# NBA public CDN assets
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


# Hardcoded team mapping (fast + reliable)
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


NBA_MCP_HTTP_TIMEOUT_SECONDS = _env_float("NBA_MCP_HTTP_TIMEOUT_SECONDS", 30.0)
NBA_MCP_MAX_CONCURRENCY = max(1, _env_int("NBA_MCP_MAX_CONCURRENCY", 8))
NBA_MCP_RETRIES = max(0, _env_int("NBA_MCP_RETRIES", 2))
NBA_MCP_CACHE_TTL_SECONDS = max(0.0, _env_float("NBA_MCP_CACHE_TTL_SECONDS", 120.0))
NBA_MCP_LIVE_CACHE_TTL_SECONDS = max(0.0, _env_float("NBA_MCP_LIVE_CACHE_TTL_SECONDS", 5.0))

NBA_MCP_TLS_VERIFY = os.getenv("NBA_MCP_TLS_VERIFY", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

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
            logger.warning(f"TLS permission error, falling back to system SSLContext: {e}")
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


_request_semaphore: Optional[asyncio.Semaphore] = None


def _get_request_semaphore() -> asyncio.Semaphore:
    global _request_semaphore
    if _request_semaphore is None:
        _request_semaphore = asyncio.Semaphore(NBA_MCP_MAX_CONCURRENCY)
    return _request_semaphore


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: Any


_cache: dict[str, _CacheEntry] = {}
_cache_lock: Optional[asyncio.Lock] = None
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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "N/A") else default
    except (ValueError, TypeError):
        return default


async def fetch_nba_data(url: str, params: Optional[dict] = None) -> Optional[dict]:
    ttl = _cache_ttl_for_url(url)
    key = _cache_key(url, params)

    if ttl > 0:
        now = time.monotonic()
        async with _get_cache_lock():
            neg_exp = _negative_cache.get(key)
            if neg_exp and neg_exp > now:
                return None
            if neg_exp:
                _negative_cache.pop(key, None)
            entry = _cache.get(key)
            if entry and entry.expires_at > now:
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
                delay = retry_after if retry_after is not None else (0.5 * (2**attempt)) + random.random() * 0.2  # nosec B311
                logger.warning(f"HTTP {status}; retrying in {delay:.2f}s ({attempt + 1}/{NBA_MCP_RETRIES})")
                await asyncio.sleep(delay)
                attempt += 1
                continue
            if status == 403 and url.startswith(NBA_LIVE_API):
                logger.info(f"HTTP 403 from live endpoint (will fall back): {url}")
                neg_ttl = max(10.0, float(ttl or 0.0))
                async with _get_cache_lock():
                    _negative_cache[key] = time.monotonic() + neg_ttl
                return None
            logger.error(f"HTTP error fetching {url}: {e}")
            return None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            if attempt >= NBA_MCP_RETRIES:
                break
            delay = (0.5 * (2**attempt)) + random.random() * 0.2  # nosec B311
            logger.warning(f"Network error; retrying in {delay:.2f}s ({attempt + 1}/{NBA_MCP_RETRIES}): {e}")
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


# ==================== Name Resolution ====================

_PLAYER_LOOKUP_TTL = 600.0  # 10 minutes


async def _fetch_player_lookup() -> list[tuple[int, str, int]]:
    """Fetch and cache the full player list. Returns [(player_id, name, is_active), ...]."""
    cache_key = "__player_lookup__"
    now = time.monotonic()
    async with _get_cache_lock():
        entry = _cache.get(cache_key)
        if entry and entry.expires_at > now:
            return entry.value

    url = f"{NBA_STATS_API}/commonallplayers"
    params = {"LeagueID": "00", "Season": get_current_season(), "IsOnlyCurrentSeason": "0"}
    data = await fetch_nba_data(url, params)

    if not data:
        # Return stale cache if available
        async with _get_cache_lock():
            entry = _cache.get(cache_key)
            if entry:
                return entry.value
        return []

    rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
    entries: list[tuple[int, str, int]] = []
    for row in rows:
        pid = _to_int(row[0] if isinstance(row, list) and row else None)
        if pid is None:
            continue
        name = str(row[2]) if len(row) > 2 else ""
        is_active = int(row[11]) if len(row) > 11 and str(row[11]).isdigit() else 1
        entries.append((pid, name, is_active))

    async with _get_cache_lock():
        _cache[cache_key] = _CacheEntry(expires_at=now + _PLAYER_LOOKUP_TTL, value=entries)
    return entries


async def _resolve_player(player_id_or_name: str) -> tuple[int, str]:
    """Resolve player name or numeric ID to (player_id, resolved_name).
    Raises ValueError if not found."""
    val = player_id_or_name.strip()
    if not val:
        raise ValueError("Please provide a player name or ID.")
    # Fast path: numeric ID
    numeric = _to_int(val)
    if numeric is not None:
        return (numeric, val)
    # Slow path: fuzzy name match
    q = val.lower()
    entries = await _fetch_player_lookup()
    if not entries:
        raise ValueError(f"Could not resolve player '{val}': player lookup unavailable.")
    matches: list[tuple[float, int, int, str]] = []
    for pid, name, is_active in entries:
        name_l = name.lower()
        score = 1.0 if q in name_l else difflib.SequenceMatcher(None, q, name_l).ratio()
        if score >= 0.35:
            matches.append((score, is_active, pid, name))
    matches.sort(key=lambda x: (x[1], x[0]), reverse=True)
    if not matches:
        raise ValueError(f"No player matched '{val}'. Try a different spelling.")
    return (matches[0][2], matches[0][3])


def _resolve_team(team_id_or_name: str) -> tuple[int, str]:
    """Resolve team name or numeric ID to (team_id, team_name).
    Raises ValueError if not found. Purely local — no API call."""
    val = team_id_or_name.strip()
    if not val:
        raise ValueError("Please provide a team name or ID.")
    numeric = _to_int(val)
    if numeric is not None:
        return (numeric, NBA_TEAMS.get(numeric, f"Team {numeric}"))
    tid = _best_team_id_from_query(val)
    if tid is None:
        raise ValueError(f"No team matched '{val}'. Try a city or nickname (e.g., 'Lakers', 'Boston').")
    return (tid, NBA_TEAMS[tid])


# ==================== Text Cleaning ====================

_TEXT_NOISE_PATTERNS = [
    re.compile(r"\s*\|\s*Player ID:\s*\d+"),
    re.compile(r"\s*\|\s*Headshot:\s*https?://\S+"),
    re.compile(r"\s*\|\s*Thumb:\s*https?://\S+"),
    re.compile(r"\s*\|\s*Logo:\s*https?://\S+"),
    re.compile(r"Player ID:\s*\d+\n?"),
    re.compile(r"Team ID:\s*\d+\s*\|?\s*"),
    re.compile(r"Headshot \(\d+x\d+\):\s*https?://\S+\n?"),
    re.compile(r"Headshot:\s*https?://\S+\n?"),
    re.compile(r"Logo:\s*https?://\S+\n?"),
    re.compile(r"\(ID:\s*\d+\)"),
    re.compile(r"Home Team ID:\s*\d+\s*\|?\s*"),
    re.compile(r"Away Team ID:\s*\d+\s*\|?\s*"),
]


def _clean_text(text: str) -> str:
    for pat in _TEXT_NOISE_PATTERNS:
        text = pat.sub("", text)
    text = re.sub(r"\|\s*\|", "|", text)
    text = re.sub(r"\|\s*\n", "\n", text)
    text = re.sub(r"^\s*\|\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ==================== JSON Wrapping ====================


def _extract_entities(text: str, arguments: Any) -> dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    players: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = []
    games: list[dict[str, Any]] = []

    if args.get("player_id"):
        pid = str(args["player_id"]).strip()
        players.append({
            "player_id": pid,
            "headshot_url": get_player_headshot_url(pid),
            "thumbnail_url": get_player_headshot_thumbnail_url(pid),
        })
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
        players.append({
            "player_id": pid,
            "headshot_url": get_player_headshot_url(pid),
            "thumbnail_url": get_player_headshot_thumbnail_url(pid),
        })

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
    *, tool_name: str, arguments: Any, text: str = "", data: Optional[dict] = None,
    error: Optional[str] = None,
) -> list[TextContent]:
    payload: dict[str, Any] = {
        "entity_type": "tool_result",
        "schema_version": "3.0",
        "tool_name": tool_name,
        "arguments": arguments if isinstance(arguments, dict) else {},
    }
    if error:
        payload["error"] = error
    if text is not None:
        payload["entities"] = _extract_entities(text, arguments)
        payload["text"] = _clean_text(text)
    if data is not None:
        payload["data"] = data
    return [
        TextContent(
            type="text", text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    ]


async def _tool_impl(tool_name: str, arguments: dict[str, Any], fn) -> list[TextContent]:
    """Execute tool logic, wrap result in JSON envelope, handle errors."""
    try:
        result = await fn()
        if isinstance(result, tuple) and len(result) == 2:
            text, data = result
        else:
            text, data = result, None
        return _wrap_tool_result(tool_name=tool_name, arguments=arguments, text=text, data=data)
    except Exception as e:
        logger.error(f"Error in {tool_name}: {e}", exc_info=True)
        return _wrap_tool_result(
            tool_name=tool_name, arguments=arguments, error=f"{type(e).__name__}: {e}"
        )


# ==================== Scoreboard Helpers ====================


async def _get_scoreboard_games_stats_api(date_obj: datetime) -> Optional[list[dict[str, Any]]]:
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
        status = safe_get(row, status_text_idx, default="Unknown") if status_text_idx >= 0 else "Unknown"
        games.append({
            "game_id": game_id,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_name": _team_name_from_id(home_id),
            "away_name": _team_name_from_id(away_id),
            "home_score": scores.get((game_id, home_id), "N/A"),
            "away_score": scores.get((game_id, away_id), "N/A"),
            "status": status,
        })
    return games


def _parse_live_scoreboard_games(data: dict) -> list[dict[str, Any]]:
    """Parse live API scoreboard data into a standard game list."""
    scoreboard = safe_get(data, "scoreboard")
    if not scoreboard or scoreboard == "N/A":
        return []
    games = safe_get(scoreboard, "games", default=[])
    if not games or games == "N/A":
        return []
    result = []
    for game in games:
        ht = safe_get(game, "homeTeam", default={})
        at = safe_get(game, "awayTeam", default={})
        result.append({
            "game_id": safe_get(game, "gameId", default="N/A"),
            "home_team_id": safe_get(ht, "teamId", default=""),
            "away_team_id": safe_get(at, "teamId", default=""),
            "home_name": safe_get(ht, "teamName", default="Home"),
            "away_name": safe_get(at, "teamName", default="Away"),
            "home_score": safe_get(ht, "score", default=0),
            "away_score": safe_get(at, "score", default=0),
            "status": safe_get(game, "gameStatusText", default="Unknown"),
            "period": safe_get(game, "period", default=0),
        })
    return result


# ==================== Tools ====================


@mcp.tool(description="Resolve team name/city/nickname to team_id. Returns top matches.")
async def resolve_team_id(query: str, limit: int = 5) -> list[TextContent]:
    _args: dict[str, Any] = {"query": query, "limit": limit}

    async def _impl():
        q = query.strip().lower()
        if not q:
            return "Please provide a non-empty team query.", None
        scored: list[tuple[float, int, str]] = []
        for tid, tname in NBA_TEAMS.items():
            name_l = tname.lower()
            score = 1.0 if q in name_l else difflib.SequenceMatcher(None, q, name_l).ratio()
            scored.append((score, tid, tname))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [s for s in scored if s[0] >= 0.3][: max(1, limit)]
        if not top:
            return f"No teams matched '{query}'.", None
        best = top[0]
        text = f"Best match: {best[2]} (ID: {best[1]})"
        if len(top) > 1:
            text += f" + {len(top) - 1} more"
        data = {"matches": [{"team_id": tid, "team_name": tn, "score": round(sc, 2)} for sc, tid, tn in top]}
        return text, data
    return await _tool_impl("resolve_team_id", _args, _impl)


@mcp.tool(description="Resolve player name to player_id. Returns top matches with active/inactive status.")
async def resolve_player_id(query: str, active_only: bool = False, limit: int = 10) -> list[TextContent]:
    _args: dict[str, Any] = {"query": query, "active_only": active_only, "limit": limit}

    async def _impl():
        query_raw = query.strip()
        q = query_raw.lower()
        if not query_raw:
            return "Please provide a non-empty player query.", None
        entries = await _fetch_player_lookup()
        if not entries:
            return "Error fetching player data.", None
        matches: list[tuple[float, int, str, int]] = []
        for pid, name, is_active in entries:
            if active_only and is_active != 1:
                continue
            name_l = name.lower()
            score = 1.0 if q in name_l else difflib.SequenceMatcher(None, q, name_l).ratio()
            if score >= 0.35:
                matches.append((score, pid, name, is_active))
        matches.sort(key=lambda x: (x[3], x[0]), reverse=True)
        top = matches[: max(1, limit)]
        if not top:
            return f"No players matched '{query_raw}'.", None
        best = top[0]
        text = f"Best match: {best[2]} (ID: {best[1]}, {'Active' if best[3] == 1 else 'Inactive'})"
        if len(top) > 1:
            text += f" + {len(top) - 1} more"
        data = {
            "matches": [
                {"player_id": pid, "name": nm, "active": ia == 1, "score": round(sc, 2)}
                for sc, pid, nm, ia in top
            ]
        }
        return text, data
    return await _tool_impl("resolve_player_id", _args, _impl)


@mcp.tool(description="Games for a date (YYYY-MM-DD or YYYYMMDD). Defaults to today.")
async def get_scoreboard(date: str | None = None) -> list[TextContent]:
    _args: dict[str, Any] = {"date": date}

    async def _impl():
        if not date or not date.strip():
            # Today's scoreboard
            url = f"{NBA_LIVE_API}/scoreboard/todaysScoreboard_00.json"
            data = await fetch_nba_data(url)
            game_list = _parse_live_scoreboard_games(data) if data else []
            if not game_list:
                today = datetime.now()
                stats_games = await _get_scoreboard_games_stats_api(today)
                if stats_games is None:
                    return "Error fetching today's scoreboard.", None
                game_list = stats_games
            display_date = datetime.now().strftime("%Y-%m-%d")
        else:
            date_str = date.strip().replace("-", "")
            try:
                date_obj = datetime.strptime(date_str, "%Y%m%d")
                display_date = date_obj.strftime("%Y-%m-%d")
            except ValueError:
                return "Invalid date format. Use YYYY-MM-DD or YYYYMMDD.", None
            url = f"{NBA_LIVE_API}/scoreboard/scoreboard_{date_str}.json"
            data = await fetch_nba_data(url)
            game_list = _parse_live_scoreboard_games(data) if data else []
            if not game_list:
                stats_games = await _get_scoreboard_games_stats_api(date_obj)
                if stats_games is None:
                    return f"No data available for {display_date}.", None
                game_list = stats_games

        if not game_list:
            return f"No games for {display_date}.", {"date": display_date, "games": []}

        lines = [f"NBA Games for {display_date} ({len(game_list)} games):"]
        for g in game_list:
            away_score = g.get("away_score", "N/A")
            home_score = g.get("home_score", "N/A")
            lines.append(f"  {g['away_name']} {away_score} @ {g['home_name']} {home_score} — {g.get('status', '')}")
        text = "\n".join(lines)
        out_data = {
            "date": display_date,
            "games": [
                {
                    "game_id": g.get("game_id"), "home_name": g.get("home_name"),
                    "away_name": g.get("away_name"), "home_score": g.get("home_score"),
                    "away_score": g.get("away_score"), "status": g.get("status"),
                    "home_team_id": g.get("home_team_id"), "away_team_id": g.get("away_team_id"),
                }
                for g in game_list
            ],
        }
        return text, out_data
    return await _tool_impl("get_scoreboard", _args, _impl)


@mcp.tool(description="Find game_id by team matchup and optional date. Accepts team names.")
async def find_game(team1: str, team2: str | None = None, date: str | None = None) -> list[TextContent]:
    _args: dict[str, Any] = {"team1": team1, "team2": team2, "date": date}

    async def _impl():
        tid1, tname1 = _resolve_team(team1)
        tid2, tname2 = (_resolve_team(team2) if team2 else (None, None))

        if date and date.strip():
            date_str = date.strip().replace("-", "")
            try:
                date_obj = datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                return "Invalid date format. Use YYYY-MM-DD or YYYYMMDD.", None

            url = f"{NBA_LIVE_API}/scoreboard/scoreboard_{date_str}.json"
            data = await fetch_nba_data(url)
            game_list = _parse_live_scoreboard_games(data) if data else []
            if not game_list:
                stats_games = await _get_scoreboard_games_stats_api(date_obj)
                game_list = stats_games or []

            for g in game_list:
                hid = _to_int(g.get("home_team_id"))
                aid = _to_int(g.get("away_team_id"))
                team_ids = {hid, aid}
                if tid2 is not None:
                    if {tid1, tid2} == team_ids:
                        text = f"Game ID: {g['game_id']} — {g['away_name']} @ {g['home_name']} ({g.get('status', '')})"
                        return text, {"game_id": g["game_id"], "home_name": g.get("home_name"), "away_name": g.get("away_name"), "status": g.get("status")}
                else:
                    if tid1 in team_ids:
                        text = f"Game ID: {g['game_id']} — {g['away_name']} @ {g['home_name']} ({g.get('status', '')})"
                        return text, {"game_id": g["game_id"], "home_name": g.get("home_name"), "away_name": g.get("away_name"), "status": g.get("status")}
            return f"No game found for {tname1}{' vs ' + tname2 if tname2 else ''} on {date_obj.strftime('%Y-%m-%d')}.", None

        # No date: search schedule for most recent
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        sched_data = await fetch_nba_data(url)
        if not sched_data:
            return "Error fetching schedule.", None
        game_dates = safe_get(sched_data, "leagueSchedule", "gameDates", default=[])
        cutoff = datetime.now().date()
        found: list[dict[str, Any]] = []
        for date_entry in game_dates:
            for game in safe_get(date_entry, "games", default=[]):
                gdt_str = safe_get(game, "gameDateTimeEst")
                if gdt_str == "N/A":
                    continue
                try:
                    gdt = datetime.fromisoformat(str(gdt_str).replace("Z", "+00:00"))
                except ValueError:
                    continue
                days_back = (cutoff - gdt.date()).days
                if days_back < 0 or days_back > 365:
                    continue
                hid = _to_int(safe_get(game, "homeTeam", "teamId"))
                aid = _to_int(safe_get(game, "awayTeam", "teamId"))
                if hid is None or aid is None:
                    continue
                if tid2 is not None:
                    if {hid, aid} != {tid1, tid2}:
                        continue
                elif tid1 not in (hid, aid):
                    continue
                status = safe_get(game, "gameStatusText", default="Unknown")
                found.append({
                    "game_id": safe_get(game, "gameId", default="N/A"),
                    "date": gdt.strftime("%Y-%m-%d"),
                    "home_name": f"{safe_get(game, 'homeTeam', 'teamCity')} {safe_get(game, 'homeTeam', 'teamName')}".strip(),
                    "away_name": f"{safe_get(game, 'awayTeam', 'teamCity')} {safe_get(game, 'awayTeam', 'teamName')}".strip(),
                    "status": status,
                })

        if not found:
            return f"No recent games found for {tname1}{' vs ' + tname2 if tname2 else ''}.", None

        # Sort: completed games first, most recent first
        def _sort(g: dict) -> tuple:
            completed = 1 if "final" in str(g.get("status", "")).lower() else 0
            return (completed, g.get("date", ""))
        found.sort(key=_sort, reverse=True)
        top = found[:5]
        lines = [f"Recent games for {tname1}{' vs ' + tname2 if tname2 else ''}:"]
        for g in top:
            lines.append(f"  {g['date']}: {g['away_name']} @ {g['home_name']} — {g['status']} (ID: {g['game_id']})")
        return "\n".join(lines), {"games": top}
    return await _tool_impl("find_game", _args, _impl)


@mcp.tool(description="Detailed game info. Accepts game_id.")
async def get_game_details(game_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            raise ValueError("Please provide a game_id.")
        url = f"{NBA_LIVE_API}/scoreboard/todaysScoreboard_00.json"
        data = await fetch_nba_data(url)
        if data:
            games = safe_get(data, "scoreboard", "games", default=[])
            game = next((g for g in games if safe_get(g, "gameId") == gid), None)
            if game:
                ht = safe_get(game, "homeTeam", default={})
                at = safe_get(game, "awayTeam", default={})
                away_stats = safe_get(at, "statistics", default={})
                home_stats = safe_get(ht, "statistics", default={})
                text = f"{safe_get(at, 'teamName')} {safe_get(at, 'score')} @ {safe_get(ht, 'teamName')} {safe_get(ht, 'score')} — {safe_get(game, 'gameStatusText')} Q{safe_get(game, 'period', default=0)}"
                game_data: dict[str, Any] = {
                    "game_id": gid,
                    "home_team": safe_get(ht, "teamName"), "away_team": safe_get(at, "teamName"),
                    "home_score": safe_get(ht, "score"), "away_score": safe_get(at, "score"),
                    "status": safe_get(game, "gameStatusText"), "period": safe_get(game, "period", default=0),
                }
                if away_stats != "N/A" and home_stats != "N/A":
                    for label, stats in [("away_stats", away_stats), ("home_stats", home_stats)]:
                        game_data[label] = {
                            "fg": f"{safe_get(stats, 'fieldGoalsMade')}/{safe_get(stats, 'fieldGoalsAttempted')}",
                            "three_pt": f"{safe_get(stats, 'threePointersMade')}/{safe_get(stats, 'threePointersAttempted')}",
                            "ft": f"{safe_get(stats, 'freeThrowsMade')}/{safe_get(stats, 'freeThrowsAttempted')}",
                            "rebounds": safe_get(stats, "reboundsTotal"),
                            "assists": safe_get(stats, "assists"),
                        }
                return text, game_data
        return f"Game {gid} not found in today's games. Use get_scoreboard to find games.", None
    return await _tool_impl("get_game_details", _args, _impl)


@mcp.tool(description="Full box score with player stats. Accepts game_id.")
async def get_box_score(game_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            raise ValueError("Please provide a game_id.")
        url = f"{NBA_LIVE_API}/boxscore/boxscore_{gid}.json"
        live_data = await fetch_nba_data(url)
        if live_data and safe_get(live_data, "game") != "N/A":
            game = safe_get(live_data, "game", default={})
            ht = safe_get(game, "homeTeam", default={})
            at = safe_get(game, "awayTeam", default={})
            text = f"Box Score: {safe_get(at, 'teamName')} {safe_get(at, 'score')} @ {safe_get(ht, 'teamName')} {safe_get(ht, 'score')}"
            box_data: dict[str, Any] = {
                "game_id": gid,
                "home_team": safe_get(ht, "teamName"), "away_team": safe_get(at, "teamName"),
                "home_score": safe_get(ht, "score"), "away_score": safe_get(at, "score"),
                "teams": {},
            }
            for label, tm in [("away", at), ("home", ht)]:
                team_name = safe_get(tm, "teamName", default=label)
                players_list = []
                for player in safe_get(tm, "players", default=[]):
                    stats = safe_get(player, "statistics", default={})
                    if stats == "N/A":
                        continue
                    minutes = safe_get(stats, "minutes", default="0:00")
                    if not minutes or minutes == "0:00":
                        continue
                    players_list.append({
                        "name": safe_get(player, "name", default="Unknown"),
                        "minutes": minutes,
                        "pts": safe_get(stats, "points", default=0),
                        "reb": safe_get(stats, "reboundsTotal", default=0),
                        "ast": safe_get(stats, "assists", default=0),
                        "stl": safe_get(stats, "steals", default=0),
                        "blk": safe_get(stats, "blocks", default=0),
                    })
                box_data["teams"][team_name] = players_list
            return text, box_data

        # Stats API fallback
        url2 = f"{NBA_STATS_API}/boxscoretraditionalv2"
        params = {"GameID": gid, "StartPeriod": "0", "EndPeriod": "10", "RangeType": "0", "StartRange": "0", "EndRange": "0"}
        data = await fetch_nba_data(url2, params)
        if not data:
            return "Box score not available yet.", None
        player_rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        team_rows = safe_get(data, "resultSets", 1, "rowSet", default=[])
        if not player_rows or player_rows == "N/A":
            return f"Box score not available for game {gid}.", None
        teams_text = []
        for t in (team_rows if team_rows and team_rows != "N/A" else []):
            teams_text.append(f"{safe_get(t, 1, default='N/A')}: {safe_get(t, 24, default=0)} PTS")
        text = f"Box Score (stats API): {', '.join(teams_text)}" if teams_text else f"Box Score for {gid}"
        return text, {"game_id": gid, "player_count": len(player_rows), "source": "stats_api"}
    return await _tool_impl("get_box_score", _args, _impl)


@mcp.tool(description="Player bio/profile. Accepts player name or ID.")
async def get_player_info(player: str) -> list[TextContent]:
    _args: dict[str, Any] = {"player": player}

    async def _impl():
        pid_int, resolved_name = await _resolve_player(player)
        pid = str(pid_int)
        _args["player_id"] = pid
        url = f"{NBA_STATS_API}/commonplayerinfo"
        params = {"PlayerID": pid}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching player info.", None
        info_headers = safe_get(data, "resultSets", 0, "headers", default=[])
        player_data = safe_get(data, "resultSets", 0, "rowSet", 0, default=[])
        if not player_data or player_data == "N/A":
            return "Player not found.", None

        def _hidx(col, fallback=None):
            try:
                return info_headers.index(col)
            except Exception:
                return fallback

        name = safe_get(player_data, 3, default=resolved_name)
        info: dict[str, Any] = {"player_id": pid_int, "name": name}
        for col, key in [("JERSEY", "jersey"), ("POSITION", "position"), ("ROSTERSTATUS", "status"),
                         ("HEIGHT", "height"), ("WEIGHT", "weight"), ("BIRTHDATE", "birth_date"),
                         ("COUNTRY", "country"), ("SCHOOL", "school")]:
            idx = _hidx(col)
            if idx is not None:
                info[key] = safe_get(player_data, idx)

        ti_idx = _hidx("TEAM_ID")
        tn_idx = _hidx("TEAM_NAME")
        ta_idx = _hidx("TEAM_ABBREVIATION")
        if ti_idx is not None:
            tiv = safe_get(player_data, ti_idx, default="")
            if tiv and tiv != "N/A":
                info["team_id"] = _to_int(tiv)
                info["team_name"] = safe_get(player_data, tn_idx, default="") if tn_idx else ""
                info["team_abbr"] = safe_get(player_data, ta_idx, default="") if ta_idx else ""

        text = f"{name} | #{info.get('jersey', 'N/A')} {info.get('position', '')} | {info.get('team_name', 'N/A')}"
        return text, info
    return await _tool_impl("get_player_info", _args, _impl)


@mcp.tool(description="Player stats. stat_type: season (default), career, game_log, hustle, defense, advanced. Accepts player name or ID.")
async def get_player_stats(player: str, stat_type: str = "season", season: str | None = None) -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player": player, "stat_type": stat_type, "season": season}

    async def _impl():
        pid_int, resolved_name = await _resolve_player(player)
        pid = str(pid_int)
        _args["player_id"] = pid
        st = stat_type.lower().strip()

        if st == "season":
            return await _player_season_stats(pid, resolved_name, season)
        elif st == "career":
            return await _player_career_stats(pid, resolved_name)
        elif st == "game_log":
            return await _player_game_log(pid, resolved_name, season)
        elif st == "hustle":
            return await _player_hustle_stats(pid, resolved_name, season)
        elif st == "defense":
            return await _player_defense_stats(pid, resolved_name, season)
        elif st == "advanced":
            return await _player_advanced_stats(pid, resolved_name, season)
        else:
            return f"Unknown stat_type '{stat_type}'. Use: season, career, game_log, hustle, defense, advanced.", None
    return await _tool_impl("get_player_stats", _args, _impl)


async def _player_season_stats(pid: str, name: str, season: str) -> tuple[str, dict]:
    url = f"{NBA_STATS_API}/playercareerstats"
    params = {"PlayerID": pid, "PerMode": "PerGame"}
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching player stats.", {}
    headers = safe_get(data, "resultSets", 0, "headers", default=[])
    all_seasons = safe_get(data, "resultSets", 0, "rowSet", default=[])
    if not all_seasons:
        return f"No stats found for {name}.", {}
    season_id_idx = headers.index("SEASON_ID") if "SEASON_ID" in headers else 1
    stats_row = next((r for r in all_seasons if str(safe_get(r, season_id_idx)) == season), None)
    if not stats_row:
        return f"No stats for {name} in {season}.", {}

    def _idx(col, fb):
        try:
            return headers.index(col)
        except ValueError:
            return fb

    stats = {
        "player_id": int(pid), "name": name, "season": season, "stat_type": "season",
        "gp": safe_get(stats_row, _idx("GP", 6)),
        "min": _safe_float(safe_get(stats_row, _idx("MIN", 8))),
        "pts": _safe_float(safe_get(stats_row, _idx("PTS", 26))),
        "reb": _safe_float(safe_get(stats_row, _idx("REB", 18))),
        "ast": _safe_float(safe_get(stats_row, _idx("AST", 19))),
        "stl": _safe_float(safe_get(stats_row, _idx("STL", 21))),
        "blk": _safe_float(safe_get(stats_row, _idx("BLK", 22))),
        "fg_pct": _safe_float(safe_get(stats_row, _idx("FG_PCT", 9))),
        "fg3_pct": _safe_float(safe_get(stats_row, _idx("FG3_PCT", 12))),
        "ft_pct": _safe_float(safe_get(stats_row, _idx("FT_PCT", 15))),
    }
    text = f"{name} {season}: {stats['pts']:.1f} PPG, {stats['reb']:.1f} RPG, {stats['ast']:.1f} APG ({stats['fg_pct']*100:.1f}% FG)"
    return text, stats


async def _player_career_stats(pid: str, name: str) -> tuple[str, dict]:
    url = f"{NBA_STATS_API}/playercareerstats"
    params = {"PlayerID": pid, "PerMode": "Totals"}
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching career stats.", {}
    rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
    if not rows or rows == "N/A":
        return f"No career stats for {name}.", {}
    tg = tp = tr = ta = ts = tb = tm = 0.0
    for sr in rows:
        if len(sr) > 26:
            tg += _safe_float(sr[6])
            tm += _safe_float(sr[8])
            tr += _safe_float(sr[20])
            ta += _safe_float(sr[21])
            ts += _safe_float(sr[22])
            tb += _safe_float(sr[23])
            tp += _safe_float(sr[26])
    ppg = tp / tg if tg > 0 else 0
    rpg = tr / tg if tg > 0 else 0
    apg = ta / tg if tg > 0 else 0
    stats = {
        "player_id": int(pid), "name": name, "stat_type": "career",
        "games": int(tg), "total_points": int(tp), "total_rebounds": int(tr),
        "total_assists": int(ta), "total_steals": int(ts), "total_blocks": int(tb),
        "total_minutes": int(tm),
        "ppg": round(ppg, 1), "rpg": round(rpg, 1), "apg": round(apg, 1),
    }
    text = f"{name} career: {int(tg)} GP, {ppg:.1f} PPG, {rpg:.1f} RPG, {apg:.1f} APG ({int(tp):,} total pts)"
    return text, stats


async def _player_game_log(pid: str, name: str, season: str) -> tuple[str, dict]:
    url = f"{NBA_STATS_API}/playergamelog"
    params = {"PlayerID": pid, "Season": season, "SeasonType": "Regular Season"}
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching game log.", {}
    headers = safe_get(data, "resultSets", 0, "headers", default=[])
    games = safe_get(data, "resultSets", 0, "rowSet", default=[])
    if not games:
        return f"No games for {name} in {season}.", {}

    def _idx(col, fb):
        try:
            return headers.index(col)
        except ValueError:
            return fb

    game_list = []
    for g in games[:10]:
        game_list.append({
            "date": safe_get(g, _idx("GAME_DATE", 2)),
            "matchup": safe_get(g, _idx("MATCHUP", 3)),
            "result": safe_get(g, _idx("WL", 4)),
            "pts": safe_get(g, _idx("PTS", 24)),
            "reb": safe_get(g, _idx("REB", 18)),
            "ast": safe_get(g, _idx("AST", 19)),
            "min": safe_get(g, _idx("MIN", 5)),
        })
    text = f"{name} last {min(len(games), 10)} games ({season}): avg {sum(_safe_float(g.get('pts')) for g in game_list) / len(game_list):.1f} PPG"
    return text, {"player_id": int(pid), "name": name, "season": season, "stat_type": "game_log", "total_games": len(games), "games": game_list}


async def _player_hustle_stats(pid: str, name: str, season: str) -> tuple[str, dict]:
    url = f"{NBA_STATS_API}/leaguehustlestatsplayer"
    params = {"Season": season, "SeasonType": "Regular Season", "PerMode": "Totals"}
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching hustle stats.", {}
    rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
    ps = next((r for r in rows if str(safe_get(r, 0)) == str(pid)), None)
    if not ps:
        return f"No hustle stats for {name} in {season}.", {}
    stats = {
        "player_id": int(pid), "name": name, "season": season, "stat_type": "hustle",
        "gp": safe_get(ps, 5, default=0),
        "deflections": safe_get(ps, 10, default=0),
        "charges_drawn": safe_get(ps, 11, default=0),
        "screen_assists": safe_get(ps, 12, default=0),
        "loose_balls": safe_get(ps, 16, default=0),
        "box_outs": safe_get(ps, 23, default=0),
    }
    text = f"{name} hustle ({season}): {stats['deflections']} deflections, {stats['charges_drawn']} charges, {stats['loose_balls']} loose balls"
    return text, stats


async def _player_defense_stats(pid: str, name: str, season: str) -> tuple[str, dict]:
    url = f"{NBA_STATS_API}/leaguedashptdefend"
    params = {"Season": season, "SeasonType": "Regular Season", "PerMode": "Totals", "DefenseCategory": "Overall"}
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching defense stats.", {}
    rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
    ps = next((r for r in rows if str(safe_get(r, 0)) == str(pid)), None)
    if not ps:
        return f"No defense stats for {name} in {season}.", {}
    opp_fg_pct = _safe_float(safe_get(ps, 11, default=0))
    normal_fg_pct = _safe_float(safe_get(ps, 12, default=0))
    diff = _safe_float(safe_get(ps, 13, default=0))
    stats = {
        "player_id": int(pid), "name": name, "season": season, "stat_type": "defense",
        "games": safe_get(ps, 6, default=0),
        "opp_fg_pct": opp_fg_pct, "normal_fg_pct": normal_fg_pct, "diff": diff,
    }
    text = f"{name} defense ({season}): Opp FG% {opp_fg_pct*100:.1f}% (normal {normal_fg_pct*100:.1f}%, diff {diff*100:+.1f}%)"
    return text, stats


async def _player_advanced_stats(pid: str, name: str, season: str) -> tuple[str, dict]:
    params = {
        "PlayerID": pid, "Season": season, "SeasonType": "Regular Season",
        "MeasureType": "Advanced", "PerMode": "PerGame", "PlusMinus": "N",
        "PaceAdjust": "N", "Rank": "N", "LastNGames": "0", "Month": "0",
        "OpponentTeamID": "0", "Period": "0", "DateFrom": "", "DateTo": "",
        "GameSegment": "", "LeagueID": "00", "Location": "", "Outcome": "",
        "PORound": "0", "SeasonSegment": "", "ShotClockRange": "",
        "VsConference": "", "VsDivision": "",
    }
    url = f"{NBA_STATS_API}/playerdashboardbygeneralsplits"
    data = await fetch_nba_data(url, params=params)
    if not data:
        return "Error fetching advanced stats.", {}
    result_sets = safe_get(data, "resultSets", default=[])
    overall = next((rs for rs in result_sets if safe_get(rs, "name") == "OverallPlayerDashboard"), None)
    if not overall:
        return f"No advanced stats for {season}.", {}
    headers = safe_get(overall, "headers", default=[])
    rows = safe_get(overall, "rowSet", default=[])
    if not rows:
        return f"No advanced stats for {season}.", {}
    row = rows[0]

    def _idx(col):
        try:
            return headers.index(col)
        except ValueError:
            return None

    stats: dict[str, Any] = {"player_id": int(pid), "name": name, "season": season, "stat_type": "advanced"}
    for col, key in [("OFF_RATING", "off_rtg"), ("DEF_RATING", "def_rtg"), ("NET_RATING", "net_rtg"),
                     ("TS_PCT", "ts_pct"), ("USG_PCT", "usg_pct"), ("PIE", "pie")]:
        idx = _idx(col)
        if idx is not None:
            stats[key] = _safe_float(safe_get(row, idx))
    text = f"{name} advanced ({season}): ORtg {stats.get('off_rtg', 'N/A')}, DRtg {stats.get('def_rtg', 'N/A')}, TS% {_safe_float(stats.get('ts_pct', 0))*100:.1f}%"
    return text, stats


@mcp.tool(description="Player awards/accolades. Accepts player name or ID.")
async def get_player_awards(player: str) -> list[TextContent]:
    _args: dict[str, Any] = {"player": player}

    async def _impl():
        pid_int, resolved_name = await _resolve_player(player)
        pid = str(pid_int)
        _args["player_id"] = pid
        url = f"{NBA_STATS_API}/playerawards"
        params = {"PlayerID": pid}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching player awards.", None
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        awards = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not awards:
            return f"No awards found for {resolved_name}.", {"player_id": pid_int, "name": resolved_name, "awards": []}

        def _idx(col, fb):
            try:
                return headers.index(col)
            except Exception:
                return fb

        di = _idx("DESCRIPTION", 4)
        si = _idx("SEASON", 6)
        ti = _idx("TEAM", 3)
        first = awards[0]
        pn = f"{safe_get(first, 1)} {safe_get(first, 2)}".strip() or resolved_name
        award_list = []
        for a in awards[:50]:
            award_list.append({
                "season": safe_get(a, si),
                "award": safe_get(a, di),
                "team": safe_get(a, ti, default=""),
            })
        text = f"{pn}: {len(awards)} awards ({len(set(a['award'] for a in award_list))} unique)"
        return text, {"player_id": pid_int, "name": pn, "total_awards": len(awards), "awards": award_list}
    return await _tool_impl("get_player_awards", _args, _impl)


@mcp.tool(description="Shooting data. data_type: splits (default) or chart. Accepts player name or ID.")
async def get_shooting_data(player: str, data_type: str = "splits", season: str | None = None, game_id: str | None = None) -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player": player, "data_type": data_type, "season": season, "game_id": game_id}

    async def _impl():
        pid_int, resolved_name = await _resolve_player(player)
        pid = str(pid_int)
        _args["player_id"] = pid
        dt = data_type.lower().strip()

        if dt == "chart":
            return await _shot_chart(pid, resolved_name, season, game_id or "")
        else:
            return await _shooting_splits(pid, resolved_name, season)
    return await _tool_impl("get_shooting_data", _args, _impl)


async def _shot_chart(pid: str, name: str, season: str, game_id: str) -> tuple[str, dict]:
    params = {
        "PlayerID": pid, "Season": season, "SeasonType": "Regular Season",
        "TeamID": "0", "GameID": game_id.strip(), "Outcome": "", "Location": "",
        "Month": "0", "SeasonSegment": "", "DateFrom": "", "DateTo": "",
        "OpponentTeamID": "0", "VsConference": "", "VsDivision": "", "Position": "",
        "RookieYear": "", "GameSegment": "", "Period": "0", "LastNGames": "0",
        "ContextMeasure": "FGA",
    }
    url = f"{NBA_STATS_API}/shotchartdetail"
    data = await fetch_nba_data(url, params=params)
    if not data:
        return "Error fetching shot chart.", {}
    headers = safe_get(data, "resultSets", 0, "headers", default=[])
    shots = safe_get(data, "resultSets", 0, "rowSet", default=[])
    if not shots:
        return f"No shot data for {name} in {season}.", {}
    try:
        mi = headers.index("SHOT_MADE_FLAG")
        di = headers.index("SHOT_DISTANCE")
    except ValueError:
        return "Error parsing shot chart.", {}
    total = len(shots)
    made = sum(1 for s in shots if safe_get(s, mi) == 1)
    pct = (made / total * 100) if total > 0 else 0.0
    avg_dist = sum(_safe_float(safe_get(s, di, default=0)) for s in shots) / total if total > 0 else 0.0
    stats = {
        "player_id": int(pid), "name": name, "season": season, "data_type": "chart",
        "total_shots": total, "made": made, "fg_pct": round(pct, 1),
        "avg_distance_ft": round(avg_dist, 1),
    }
    text = f"{name} shot chart ({season}): {made}/{total} ({pct:.1f}%), avg {avg_dist:.1f} ft"
    return text, stats


async def _shooting_splits(pid: str, name: str, season: str) -> tuple[str, dict]:
    params = {
        "PlayerID": pid, "Season": season, "SeasonType": "Regular Season",
        "PerMode": "Totals", "MeasureType": "Base", "PlusMinus": "N",
        "PaceAdjust": "N", "Rank": "N", "Outcome": "", "Location": "",
        "Month": "0", "SeasonSegment": "", "DateFrom": "", "DateTo": "",
        "OpponentTeamID": "0", "VsConference": "", "VsDivision": "",
        "GameSegment": "", "Period": "0", "LastNGames": "0",
    }
    url = f"{NBA_STATS_API}/playerdashboardbyshootingsplits"
    data = await fetch_nba_data(url, params=params)
    if not data:
        return "Error fetching shooting splits.", {}
    result_sets = safe_get(data, "resultSets", default=[])
    overall = next((rs for rs in result_sets if safe_get(rs, "name") == "OverallPlayerDashboard"), None)
    if not overall:
        return f"No shooting data for {season}.", {}
    headers = safe_get(overall, "headers", default=[])
    rows = safe_get(overall, "rowSet", default=[])
    if not rows:
        return f"No shooting data for {season}.", {}
    row = rows[0]
    try:
        fgmi = headers.index("FGM")
        fgai = headers.index("FGA")
        fgpi = headers.index("FG_PCT")
        fg3mi = headers.index("FG3M")
        fg3ai = headers.index("FG3A")
        fg3pi = headers.index("FG3_PCT")
    except ValueError:
        return "Error parsing shooting splits.", {}
    stats = {
        "player_id": int(pid), "name": name, "season": season, "data_type": "splits",
        "fgm": safe_get(row, fgmi, default=0), "fga": safe_get(row, fgai, default=0),
        "fg_pct": _safe_float(safe_get(row, fgpi, default=0)),
        "fg3m": safe_get(row, fg3mi, default=0), "fg3a": safe_get(row, fg3ai, default=0),
        "fg3_pct": _safe_float(safe_get(row, fg3pi, default=0)),
    }
    text = f"{name} splits ({season}): FG {stats['fgm']}/{stats['fga']} ({stats['fg_pct']*100:.1f}%), 3P {stats['fg3m']}/{stats['fg3a']} ({stats['fg3_pct']*100:.1f}%)"
    return text, stats


@mcp.tool(description="Team roster. Accepts team name or ID.")
async def get_team_roster(team: str, season: str | None = None) -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"team": team, "season": season}

    async def _impl():
        tid_int, tname = _resolve_team(team)
        tid = str(tid_int)
        _args["team_id"] = tid
        url = f"{NBA_STATS_API}/commonteamroster"
        params = {"TeamID": tid, "Season": season}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching roster.", None
        roster_data = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not roster_data:
            return f"No roster found for {tname}.", None
        players = []
        for p in roster_data:
            players.append({
                "name": safe_get(p, 3),
                "number": safe_get(p, 4),
                "position": safe_get(p, 5),
                "player_id": safe_get(p, 14, default=""),
            })
        text = f"{tname} roster ({season}): {len(players)} players"
        return text, {"team_id": tid_int, "team_name": tname, "season": season, "players": players}
    return await _tool_impl("get_team_roster", _args, _impl)


@mcp.tool(description="League standings by conference.")
async def get_standings(season: str | None = None) -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"season": season}

    async def _impl():
        url = f"{NBA_STATS_API}/leaguestandingsv3"
        params = {"LeagueID": "00", "Season": season, "SeasonType": "Regular Season"}
        data = await fetch_nba_data(url, params)
        if not data:
            return "Error fetching standings.", None
        headers = safe_get(data, "resultSets", 0, "headers", default=[])
        rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
        if not rows:
            return "No standings found.", None

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

        def _pct(row):
            try:
                return float(safe_get(row, pi, default=0) or 0)
            except Exception:
                return 0.0

        east = sorted([r for r in rows if safe_get(r, ci, default="") == "East"], key=_pct, reverse=True)
        west = sorted([r for r in rows if safe_get(r, ci, default="") != "East"], key=_pct, reverse=True)

        def _team_entry(r):
            tid = safe_get(r, tii, default="") if tii is not None else ""
            return {
                "team_name": safe_get(r, tni), "team_id": _to_int(tid),
                "wins": safe_get(r, wi), "losses": safe_get(r, li),
                "win_pct": _safe_float(safe_get(r, pi)),
            }

        east_data = [_team_entry(r) for r in east]
        west_data = [_team_entry(r) for r in west]

        lines = [f"NBA Standings ({season}):"]
        lines.append("East:")
        for i, t in enumerate(east_data, 1):
            lines.append(f"  {i}. {t['team_name']}: {t['wins']}-{t['losses']} ({t['win_pct']:.3f})")
        lines.append("West:")
        for i, t in enumerate(west_data, 1):
            lines.append(f"  {i}. {t['team_name']}: {t['wins']}-{t['losses']} ({t['win_pct']:.3f})")

        text = "\n".join(lines)
        return text, {"season": season, "east": east_data, "west": west_data}
    return await _tool_impl("get_standings", _args, _impl)


@mcp.tool(description="League leaders. scope: current_season (default), all_time, hustle. category: points, assists, rebounds, etc.")
async def get_leaders(category: str = "points", scope: str = "current_season", season: str | None = None, limit: int = 10) -> list[TextContent]:
    season = season or get_current_season()
    limit = min(limit, 50)
    _args: dict[str, Any] = {"category": category, "scope": scope, "season": season, "limit": limit}

    async def _impl():
        sc = scope.lower().strip()
        if sc == "all_time":
            return await _all_time_leaders(category, limit)
        elif sc == "hustle":
            return await _hustle_leaders(category, season, limit)
        else:
            return await _season_leaders(category, season, limit)
    return await _tool_impl("get_leaders", _args, _impl)


async def _season_leaders(stat_type: str, season: str, limit: int) -> tuple[str, dict]:
    sm = {"points": "PTS", "assists": "AST", "rebounds": "REB", "steals": "STL", "blocks": "BLK", "fg%": "FG_PCT", "3p%": "FG3_PCT", "ft%": "FT_PCT"}
    stat_category = sm.get(stat_type.lower(), sm.get(stat_type.title(), "PTS"))
    url = f"{NBA_STATS_API}/leaguedashplayerstats"
    params = {
        "LeagueID": "00", "Season": season, "SeasonType": "Regular Season",
        "PerMode": "PerGame", "MeasureType": "Base", "PlusMinus": "N",
        "PaceAdjust": "N", "Rank": "N", "Outcome": "", "Location": "",
        "Month": "0", "SeasonSegment": "", "DateFrom": "", "DateTo": "",
        "OpponentTeamID": "0", "VsConference": "", "VsDivision": "",
        "GameSegment": "", "Period": "0", "LastNGames": "0",
    }
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching league leaders.", {}
    headers = safe_get(data, "resultSets", 0, "headers", default=[])
    rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
    if not rows or not headers:
        return f"No data for {stat_type} leaders.", {}
    try:
        pii = headers.index("PLAYER_ID")
        pni = headers.index("PLAYER_NAME")
        tai = headers.index("TEAM_ABBREVIATION")
    except ValueError:
        pii, pni, tai = 0, 1, 3
    if stat_category not in headers:
        return f"Unsupported category '{stat_type}'. Try: {', '.join(sorted(sm.keys()))}", {}
    si = headers.index(stat_category)

    sorted_rows = sorted(rows, key=lambda r: _safe_float(safe_get(r, si, default=0)), reverse=True)
    leaders = []
    for row in sorted_rows[:limit]:
        val = _safe_float(safe_get(row, si, default=0))
        leaders.append({
            "rank": len(leaders) + 1,
            "player_name": safe_get(row, pni, default="Unknown"),
            "player_id": safe_get(row, pii),
            "team": safe_get(row, tai, default=""),
            "value": val,
        })
    top = leaders[0] if leaders else {}
    text = f"{stat_type.title()} leaders ({season}): 1. {top.get('player_name', 'N/A')} ({top.get('team', '')}) {top.get('value', 0)}"
    return text, {"scope": "current_season", "category": stat_type, "season": season, "leaders": leaders}


async def _all_time_leaders(stat_category: str, limit: int) -> tuple[str, dict]:
    sc = stat_category.lower()
    sm = {
        "points": "PTSLeaders", "rebounds": "REBLeaders", "assists": "ASTLeaders",
        "steals": "STLLeaders", "blocks": "BLKLeaders", "games": "GPLeaders",
        "offensive_rebounds": "OREBLeaders", "defensive_rebounds": "DREBLeaders",
        "field_goals_made": "FGMLeaders", "field_goals_attempted": "FGALeaders",
        "field_goal_pct": "FG_PCTLeaders", "three_pointers_made": "FG3MLeaders",
        "three_pointers_attempted": "FG3ALeaders", "three_point_pct": "FG3_PCTLeaders",
        "free_throws_made": "FTMLeaders", "free_throws_attempted": "FTALeaders",
        "free_throw_pct": "FT_PCTLeaders", "turnovers": "TOVLeaders",
        "personal_fouls": "PFLeaders",
    }
    if sc not in sm:
        return f"Invalid category. Choose from: {', '.join(sorted(sm.keys()))}", {}
    url = f"{NBA_STATS_API}/alltimeleadersgrids"
    params = {"LeagueID": "00", "PerMode": "Totals", "SeasonType": "Regular Season", "TopX": str(limit)}
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching all-time leaders.", {}
    leaders_data = None
    for rs in safe_get(data, "resultSets", default=[]):
        if rs.get("name") == sm[sc]:
            leaders_data = rs.get("rowSet", [])
            break
    if not leaders_data:
        return f"No all-time leaders for {sc}.", {}
    leaders = []
    for i, p in enumerate(leaders_data, 1):
        val = safe_get(p, 2, default=0)
        leaders.append({
            "rank": i,
            "player_name": safe_get(p, 1, default="Unknown"),
            "value": val,
            "active": safe_get(p, 4, default=0) == 1,
        })
    top = leaders[0] if leaders else {}
    text = f"All-time {sc}: 1. {top.get('player_name', 'N/A')} ({top.get('value', 0)})"
    return text, {"scope": "all_time", "category": sc, "leaders": leaders}


async def _hustle_leaders(stat_category: str, season: str, limit: int) -> tuple[str, dict]:
    sm = {"deflections": (10, "Deflections"), "charges": (11, "Charges Drawn"), "screen_assists": (12, "Screen Assists"), "loose_balls": (16, "Loose Balls"), "box_outs": (23, "Box Outs")}
    sc = stat_category.lower()
    if sc not in sm:
        return f"Invalid hustle category. Choose from: {', '.join(sm.keys())}", {}
    ci, sn = sm[sc]
    url = f"{NBA_STATS_API}/leaguehustlestatsplayer"
    params = {"Season": season, "SeasonType": "Regular Season", "PerMode": "Totals"}
    data = await fetch_nba_data(url, params)
    if not data:
        return "Error fetching hustle stats.", {}
    rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
    sp = sorted(rows, key=lambda x: _safe_float(safe_get(x, ci, default=0)), reverse=True)[:limit]
    leaders = []
    for i, p in enumerate(sp, 1):
        leaders.append({
            "rank": i,
            "player_name": safe_get(p, 1, default="Unknown"),
            "team": safe_get(p, 3, default=""),
            "value": safe_get(p, ci, default=0),
        })
    top = leaders[0] if leaders else {}
    text = f"Hustle {sn} leaders ({season}): 1. {top.get('player_name', 'N/A')} ({top.get('value', 0)})"
    return text, {"scope": "hustle", "category": sc, "season": season, "leaders": leaders}


@mcp.tool(description="Team upcoming schedule. Accepts team name or ID.")
async def get_schedule(team: str, days_ahead: int = 7) -> list[TextContent]:
    days_ahead = min(days_ahead, 90)
    _args: dict[str, Any] = {"team": team, "days_ahead": days_ahead}

    async def _impl():
        tid_int, tname = _resolve_team(team)
        tid = str(tid_int)
        _args["team_id"] = tid
        url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
        data = await fetch_nba_data(url)
        if not data:
            return "Error fetching schedule.", None
        game_dates = safe_get(data, "leagueSchedule", "gameDates", default=[])
        if not game_dates:
            return "No schedule data available.", None
        today = datetime.now()
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
                        upcoming.append({
                            "date": gd.strftime("%Y-%m-%d"),
                            "home_team": f"{safe_get(game, 'homeTeam', 'teamCity')} {safe_get(game, 'homeTeam', 'teamName')}".strip(),
                            "away_team": f"{safe_get(game, 'awayTeam', 'teamCity')} {safe_get(game, 'awayTeam', 'teamName')}".strip(),
                            "arena": safe_get(game, "arenaName"),
                            "game_id": safe_get(game, "gameId"),
                        })
        upcoming.sort(key=lambda x: x["date"])
        if not upcoming:
            return f"No games for {tname} in the next {days_ahead} days.", {"team": tname, "games": []}
        text = f"{tname} next {len(upcoming)} games (next {days_ahead} days)"
        return text, {"team_id": tid_int, "team_name": tname, "games": upcoming}
    return await _tool_impl("get_schedule", _args, _impl)


@mcp.tool(description="Major award winners for a season (MVP, etc.).")
async def get_season_awards(season: str | None = None) -> list[TextContent]:
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
            return f"Award data for {season} not available. Use get_player_awards for individual awards.", None
        mvp_name, tid = mvp_map[season]
        text = f"{season} MVP: {mvp_name}"
        return text, {"season": season, "mvp": mvp_name, "mvp_team_id": tid}
    return await _tool_impl("get_season_awards", _args, _impl)


@mcp.tool(description="Team advanced metrics (ORtg, DRtg, pace, net rating). Accepts team name or ID.")
async def get_team_advanced_stats(team: str, season: str | None = None) -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"team": team, "season": season}

    async def _impl():
        tid_int, tname = _resolve_team(team)
        tid = str(tid_int)
        _args["team_id"] = tid
        params = {
            "TeamID": tid, "Season": season, "SeasonType": "Regular Season",
            "MeasureType": "Advanced", "PerMode": "PerGame", "PlusMinus": "N",
            "PaceAdjust": "N", "Rank": "N", "LastNGames": "0", "Month": "0",
            "OpponentTeamID": "0", "Period": "0", "DateFrom": "", "DateTo": "",
            "GameSegment": "", "LeagueID": "00", "Location": "", "Outcome": "",
            "PORound": "0", "SeasonSegment": "", "ShotClockRange": "",
            "VsConference": "", "VsDivision": "",
        }
        url = f"{NBA_STATS_API}/teamdashboardbygeneralsplits"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Error fetching team advanced stats.", None
        result_sets = safe_get(data, "resultSets", default=[])
        overall = next((rs for rs in result_sets if safe_get(rs, "name") == "OverallTeamDashboard"), None)
        if not overall:
            return f"No advanced stats for {season}.", None
        headers = safe_get(overall, "headers", default=[])
        rows = safe_get(overall, "rowSet", default=[])
        if not rows:
            return f"No advanced stats for {season}.", None
        row = rows[0]

        def _idx(col):
            try:
                return headers.index(col)
            except ValueError:
                return None

        stats: dict[str, Any] = {"team_id": tid_int, "team_name": tname, "season": season}
        for col, key in [("OFF_RATING", "off_rtg"), ("DEF_RATING", "def_rtg"), ("NET_RATING", "net_rtg"), ("PACE", "pace")]:
            idx = _idx(col)
            if idx is not None:
                stats[key] = _safe_float(safe_get(row, idx))
        text = f"{tname} advanced ({season}): ORtg {stats.get('off_rtg', 'N/A')}, DRtg {stats.get('def_rtg', 'N/A')}, Pace {stats.get('pace', 'N/A')}"
        return text, stats
    return await _tool_impl("get_team_advanced_stats", _args, _impl)


@mcp.tool(description="Play-by-play data for a game.")
async def get_play_by_play(game_id: str, start_period: int = 1, end_period: int = 10) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id, "start_period": start_period, "end_period": end_period}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            raise ValueError("Please provide a game_id.")
        params = {"GameID": gid, "StartPeriod": start_period, "EndPeriod": end_period}
        url = f"{NBA_STATS_API}/playbyplayv2"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Error fetching play-by-play.", None
        result_sets = safe_get(data, "resultSets", default=[])
        pbp = next((rs for rs in result_sets if safe_get(rs, "name") == "PlayByPlay"), None)
        if not pbp:
            return f"No play-by-play data for game {gid}.", None
        headers = safe_get(pbp, "headers", default=[])
        plays = safe_get(pbp, "rowSet", default=[])
        if not plays:
            return f"No plays for game {gid}.", None

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

        play_list = []
        for play in plays[:25]:
            desc = safe_get(play, hdi, default="") or safe_get(play, vdi, default="")
            if not desc or desc == "N/A":
                continue
            play_list.append({
                "period": safe_get(play, pi, default=""),
                "time": safe_get(play, ti, default=""),
                "description": desc,
                "score": safe_get(play, sci, default=""),
            })
        text = f"Play-by-play for {gid}: {len(plays)} total plays (showing {len(play_list)})"
        return text, {"game_id": gid, "total_plays": len(plays), "plays": play_list}
    return await _tool_impl("get_play_by_play", _args, _impl)


@mcp.tool(description="Player rotation/substitution data for a game.")
async def get_game_rotation(game_id: str) -> list[TextContent]:
    _args: dict[str, Any] = {"game_id": game_id}

    async def _impl():
        gid = game_id.strip()
        if not gid:
            raise ValueError("Please provide a game_id.")
        params = {"GameID": gid, "LeagueID": "00"}
        url = f"{NBA_STATS_API}/gamerotation"
        data = await fetch_nba_data(url, params=params)
        if not data:
            return "Error fetching rotation data.", None
        result_sets = safe_get(data, "resultSets", default=[])
        away = next((rs for rs in result_sets if safe_get(rs, "name") == "AwayTeam"), None)
        home = next((rs for rs in result_sets if safe_get(rs, "name") == "HomeTeam"), None)
        if not away and not home:
            return f"No rotation data for game {gid}.", None

        def _team_rotation(team_data):
            headers = safe_get(team_data, "headers", default=[])
            rows = safe_get(team_data, "rowSet", default=[])
            if not rows:
                return []
            try:
                fi = headers.index("PLAYER_FIRST")
                li = headers.index("PLAYER_LAST")
                pi = headers.index("PLAYER_PTS")
            except ValueError:
                return []
            pts_by: dict[str, int] = {}
            for r in rows:
                n = f"{safe_get(r, fi, default='')} {safe_get(r, li, default='')}".strip()
                try:
                    pts_by[n] = max(pts_by.get(n, 0), int(_safe_float(safe_get(r, pi, default=0))))
                except Exception:
                    pass
            return sorted([{"name": n, "pts": p} for n, p in pts_by.items()], key=lambda x: x["pts"], reverse=True)

        rotation_data: dict[str, Any] = {"game_id": gid}
        if away:
            rotation_data["away"] = _team_rotation(away)
        if home:
            rotation_data["home"] = _team_rotation(home)
        text = f"Rotation for {gid}: {len(rotation_data.get('away', []))} away players, {len(rotation_data.get('home', []))} home players"
        return text, rotation_data
    return await _tool_impl("get_game_rotation", _args, _impl)


# ==================== Composite Tools ====================


@mcp.tool(description="Compare two players side by side. Accepts names or IDs.")
async def compare_players(player1: str, player2: str, season: str | None = None) -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"player1": player1, "player2": player2, "season": season}

    async def _impl():
        pid1_int, name1 = await _resolve_player(player1)
        pid2_int, name2 = await _resolve_player(player2)
        pid1, pid2 = str(pid1_int), str(pid2_int)

        url = f"{NBA_STATS_API}/playercareerstats"
        r1, r2 = await asyncio.gather(
            fetch_nba_data(url, {"PlayerID": pid1, "PerMode": "PerGame"}),
            fetch_nba_data(url, {"PlayerID": pid2, "PerMode": "PerGame"}),
        )

        def _extract_season(data, pid, name, szn):
            if not data:
                return {"name": name, "player_id": int(pid), "error": "No data"}
            headers = safe_get(data, "resultSets", 0, "headers", default=[])
            rows = safe_get(data, "resultSets", 0, "rowSet", default=[])
            if not rows:
                return {"name": name, "player_id": int(pid), "error": "No stats"}
            sid_idx = headers.index("SEASON_ID") if "SEASON_ID" in headers else 1
            row = next((r for r in rows if str(safe_get(r, sid_idx)) == szn), None)
            if not row:
                return {"name": name, "player_id": int(pid), "error": f"No data for {szn}"}

            def _idx(col, fb):
                try:
                    return headers.index(col)
                except ValueError:
                    return fb
            return {
                "name": name, "player_id": int(pid), "season": szn,
                "gp": safe_get(row, _idx("GP", 6)),
                "pts": _safe_float(safe_get(row, _idx("PTS", 26))),
                "reb": _safe_float(safe_get(row, _idx("REB", 18))),
                "ast": _safe_float(safe_get(row, _idx("AST", 19))),
                "stl": _safe_float(safe_get(row, _idx("STL", 21))),
                "blk": _safe_float(safe_get(row, _idx("BLK", 22))),
                "fg_pct": _safe_float(safe_get(row, _idx("FG_PCT", 9))),
                "fg3_pct": _safe_float(safe_get(row, _idx("FG3_PCT", 12))),
                "ft_pct": _safe_float(safe_get(row, _idx("FT_PCT", 15))),
            }

        s1 = _extract_season(r1, pid1, name1, season)
        s2 = _extract_season(r2, pid2, name2, season)

        p1_pts = s1.get("pts", 0) if "error" not in s1 else "N/A"
        p2_pts = s2.get("pts", 0) if "error" not in s2 else "N/A"
        text = f"{name1} vs {name2} ({season}): {p1_pts} PPG vs {p2_pts} PPG"
        return text, {"season": season, "player1": s1, "player2": s2}
    return await _tool_impl("compare_players", _args, _impl)


@mcp.tool(description="All games + scores for a date. Defaults to today.")
async def daily_summary(date: str | None = None) -> list[TextContent]:
    _args: dict[str, Any] = {"date": date}

    async def _impl():
        if not date or not date.strip():
            display_date = datetime.now().strftime("%Y-%m-%d")
            url = f"{NBA_LIVE_API}/scoreboard/todaysScoreboard_00.json"
            data = await fetch_nba_data(url)
            game_list = _parse_live_scoreboard_games(data) if data else []
            if not game_list:
                today = datetime.now()
                stats_games = await _get_scoreboard_games_stats_api(today)
                game_list = stats_games or []
        else:
            date_str = date.strip().replace("-", "")
            try:
                date_obj = datetime.strptime(date_str, "%Y%m%d")
                display_date = date_obj.strftime("%Y-%m-%d")
            except ValueError:
                return "Invalid date format. Use YYYY-MM-DD or YYYYMMDD.", None
            url = f"{NBA_LIVE_API}/scoreboard/scoreboard_{date_str}.json"
            data = await fetch_nba_data(url)
            game_list = _parse_live_scoreboard_games(data) if data else []
            if not game_list:
                stats_games = await _get_scoreboard_games_stats_api(date_obj)
                game_list = stats_games or []

        if not game_list:
            return f"No games for {display_date}.", {"date": display_date, "games": []}

        lines = [f"NBA Daily Summary {display_date} — {len(game_list)} games:"]
        for g in game_list:
            lines.append(f"  {g.get('away_name', 'Away')} {g.get('away_score', 'N/A')} @ {g.get('home_name', 'Home')} {g.get('home_score', 'N/A')} — {g.get('status', '')}")
        text = "\n".join(lines)
        return text, {
            "date": display_date,
            "game_count": len(game_list),
            "games": [
                {
                    "game_id": g.get("game_id"), "home_name": g.get("home_name"),
                    "away_name": g.get("away_name"), "home_score": g.get("home_score"),
                    "away_score": g.get("away_score"), "status": g.get("status"),
                }
                for g in game_list
            ],
        }
    return await _tool_impl("daily_summary", _args, _impl)


@mcp.tool(description="Team overview: roster + record + upcoming schedule. Accepts team name or ID.")
async def team_overview(team: str, season: str | None = None) -> list[TextContent]:
    season = season or get_current_season()
    _args: dict[str, Any] = {"team": team, "season": season}

    async def _impl():
        tid_int, tname = _resolve_team(team)
        tid = str(tid_int)
        _args["team_id"] = tid

        # Fetch roster, standings, schedule in parallel
        roster_fut = fetch_nba_data(f"{NBA_STATS_API}/commonteamroster", {"TeamID": tid, "Season": season})
        standings_fut = fetch_nba_data(f"{NBA_STATS_API}/leaguestandingsv3", {"LeagueID": "00", "Season": season, "SeasonType": "Regular Season"})
        sched_fut = fetch_nba_data("https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json")

        roster_data, standings_data, sched_data = await asyncio.gather(roster_fut, standings_fut, sched_fut)

        overview: dict[str, Any] = {"team_id": tid_int, "team_name": tname, "season": season}

        # Roster
        if roster_data:
            players = safe_get(roster_data, "resultSets", 0, "rowSet", default=[])
            overview["roster"] = [{"name": safe_get(p, 3), "number": safe_get(p, 4), "position": safe_get(p, 5)} for p in players]
        else:
            overview["roster"] = []

        # Record from standings
        if standings_data:
            headers = safe_get(standings_data, "resultSets", 0, "headers", default=[])
            rows = safe_get(standings_data, "resultSets", 0, "rowSet", default=[])

            def _idx(col, fb=None):
                try:
                    return headers.index(col)
                except Exception:
                    return fb

            tii = _idx("TeamID", _idx("TEAM_ID"))
            wi = _idx("WINS", 13)
            li = _idx("LOSSES", 14)
            pi = _idx("WinPCT", 15)
            ci = _idx("Conference", 5)
            for r in rows:
                row_tid = _to_int(safe_get(r, tii)) if tii is not None else None
                if row_tid == tid_int:
                    overview["record"] = {
                        "wins": safe_get(r, wi), "losses": safe_get(r, li),
                        "win_pct": _safe_float(safe_get(r, pi)),
                        "conference": safe_get(r, ci, default=""),
                    }
                    break

        # Schedule (next 7 days)
        if sched_data:
            game_dates = safe_get(sched_data, "leagueSchedule", "gameDates", default=[])
            today = datetime.now()
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
                        if gd.date() >= today.date() and (gd.date() - today.date()).days <= 7:
                            upcoming.append({
                                "date": gd.strftime("%Y-%m-%d"),
                                "opponent": f"{safe_get(game, 'awayTeam', 'teamName')}" if hid == tid_int else f"@ {safe_get(game, 'homeTeam', 'teamName')}",
                                "game_id": safe_get(game, "gameId"),
                            })
            upcoming.sort(key=lambda x: x["date"])
            overview["upcoming"] = upcoming

        record = overview.get("record", {})
        text = f"{tname} ({season}): {record.get('wins', '?')}-{record.get('losses', '?')} | {len(overview.get('roster', []))} players | {len(overview.get('upcoming', []))} upcoming games"
        return text, overview
    return await _tool_impl("team_overview", _args, _impl)


# ==================== Entrypoints ====================


async def async_main() -> None:
    await mcp.run_stdio_async()


def main() -> None:
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
