"""Thin async client for the remote Untappd MCP server (JSON-RPC 2.0 over HTTP).

The server responds with an SSE-shaped body (`event: message\\ndata: {...}`)
even for a single-shot POST, so responses are parsed accordingly rather than
as plain JSON.

Every function takes the caller's own Untappd MCP token as its first
parameter - there is no module-level token. Each Telegram user who connects
their own account (see user_tokens.py) gets their own check-ins/searches
attributed to their own account; the shared UNTAPPD_MCP_URL endpoint is the
only thing that stays global.
"""

import itertools
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_id_counter = itertools.count(1)


class UntappdMCPError(Exception):
    """Raised when the MCP server itself returns an error payload."""


class UntappdRateLimited(UntappdMCPError):
    """Raised on HTTP 429 or an Untappd-side rate-limit error.

    Callers must not retry automatically - the shared Untappd quota has been
    observed to stay throttled well past a few seconds of backoff.
    """


class UntappdMalformedResponse(UntappdMCPError):
    """Raised when a response's JSON can't be parsed at all, even after the
    control-character/multi-line tolerance in _parse_sse_json and _call_tool.

    Observed in practice: a specific beer/brewery/venue name that the
    third-party MCP server itself serializes into genuinely broken JSON
    (e.g. an unescaped quote), not something any client-side parsing fix
    can repair. Distinct from UntappdMCPError so a paginating caller (see
    webapp_server.py's backfill loops) can choose to skip past the exact
    poisoned page instead of retrying the identical request forever.
    """


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


def _parse_sse_json(body: str) -> dict:
    # This server sends exactly one JSON-RPC response per POST (see module
    # docstring), so there is exactly one SSE event to extract - do NOT stop
    # at the first blank line as an "event terminator" (an earlier version
    # of this function did, and it was wrong): real beer/brewery/checkin-
    # comment text has been observed to contain literal blank lines
    # (paragraph breaks) as raw, unescaped newlines embedded directly in the
    # JSON text, and stopping there silently truncated the JSON mid-string.
    # Take everything from the first "data:" line to the true end of the
    # body instead. A continuation line that happens to be re-prefixed with
    # "data:" (spec-compliant multi-line SSE - not observed from this
    # server in practice, handled defensively anyway) gets that prefix
    # stripped; everything else is kept verbatim, restoring any embedded
    # raw newline exactly as it was - then parsed with strict=False to
    # tolerate it.
    lines = body.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("data:")), None)
    if start is None:
        raise UntappdMCPError(f"No 'data:' line in MCP response: {body[:200]!r}")
    pieces = [lines[start][len("data:"):].lstrip()]
    for line in lines[start + 1:]:
        pieces.append(line[len("data:"):].lstrip() if line.startswith("data:") else line)
    payload = "\n".join(pieces)
    try:
        return json.loads(payload, strict=False)
    except json.JSONDecodeError as e:
        raise UntappdMalformedResponse(f"Malformed MCP response JSON: {e}") from e


async def _call_tool(name: str, arguments: dict, token: str) -> dict:
    # Read lazily (not at module import time) so this doesn't silently break
    # if some other module imports untappd_mcp before .env is loaded - it
    # already has, once, the hard way.
    mcp_url = os.environ.get("UNTAPPD_MCP_URL", "")
    if not mcp_url:
        raise UntappdMCPError("UNTAPPD_MCP_URL not configured")
    if not token:
        raise UntappdMCPError("no Untappd token provided")

    payload = {
        "jsonrpc": "2.0",
        "id": next(_id_counter),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    client = _get_client()
    try:
        resp = await client.post(
            mcp_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
    except httpx.HTTPError as e:
        raise UntappdMCPError(f"MCP request failed: {e}") from e

    if resp.status_code == 429:
        raise UntappdRateLimited("Untappd MCP returned HTTP 429")
    if resp.status_code == 401:
        raise UntappdMCPError("Untappd MCP rejected the token (401 Unauthorized)")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UntappdMCPError(f"MCP call {name!r} failed: {e}") from e

    envelope = _parse_sse_json(resp.text)
    result = envelope.get("result")
    if result is None:
        raise UntappdMCPError(f"MCP call {name!r} returned no result: {envelope}")

    content = result.get("content") or []
    text = next((c["text"] for c in content if c.get("type") == "text"), None)
    if text is None:
        raise UntappdMCPError(f"MCP call {name!r} returned no text content: {result}")

    # strict=False: real beer/brewery/venue names have been observed to
    # contain raw, unescaped control characters (see _parse_sse_json's
    # comment above) - this inner tool-result JSON is just as exposed to
    # that as the outer SSE envelope was.
    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        raise UntappdMalformedResponse(f"Malformed MCP tool-result JSON: {e}") from e
    if isinstance(parsed, dict) and parsed.get("error"):
        message = str(parsed.get("message", parsed["error"]))
        if "429" in message or "rate" in message.lower():
            raise UntappdRateLimited(message)
        raise UntappdMCPError(message)
    return parsed


def _current_tz_and_offset() -> tuple[str, float]:
    """Timezone name + current UTC offset in hours, from BOT_TIMEZONE/TZ."""
    tz_name = os.getenv("BOT_TIMEZONE") or os.getenv("TZ") or "Europe/Warsaw"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning("Unknown BOT_TIMEZONE/TZ=%r; falling back to UTC", tz_name)
        tz_name, tz = "UTC", ZoneInfo("UTC")
    offset = datetime.now(tz).utcoffset()
    gmt_offset = (offset.total_seconds() / 3600.0) if offset else 0.0
    return tz_name, gmt_offset


async def get_my_profile(token: str) -> dict:
    """Validates a token and identifies whose account it is.

    Returns the raw envelope, e.g. {"user": {"uid":.., "user_name":..,
    "first_name":.., ...}}. Used by /connect_untappd to confirm a pasted
    token works and to greet the person by their real Untappd username.
    """
    return await _call_tool("get_my_profile", {}, token)


async def search_beers(token: str, query: str, limit: int = 20) -> list[dict]:
    result = await _call_tool("search_beers", {"query": query, "limit": limit}, token)
    return result if isinstance(result, list) else []


async def get_user_beers(
    token: str,
    username: str,
    sort: str = "date",
    limit: int = 50,
    offset: int = 0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Bulk list of the given user's distinct checked-in beers (one quota
    call covers up to 50 beers, vs. one call each via check_i_had_beer).
    Returns the raw envelope: {"total_count", "beers": {"count", "items": [...]}}.

    NOTE: `start_date`/`end_date` were tried as a "beers I had recently"
    substitute for per-beer check_i_had_beer calls and found unreliable -
    beers independently confirmed hadIt=true via check_i_had_beer did not
    appear even in a 12-day/166-result window. Cause not evident from the
    documented API behavior (possibly filters/sorts by first-ever-tried
    date rather than most recent check-in). Don't rely on this for "have I
    had this beer recently" without re-verifying against a real account.
    """
    arguments: dict = {"username": username, "sort": sort, "limit": limit, "offset": offset}
    if start_date:
        arguments["startDate"] = start_date
    if end_date:
        arguments["endDate"] = end_date
    return await _call_tool("get_user_beers", arguments, token)


async def check_i_had_beer(token: str, beer_id: int) -> dict:
    """Whether the connected account has ever checked in this beer. Costs 1 quota call."""
    return await _call_tool("check_i_had_beer", {"beerId": beer_id}, token)


async def get_my_wishlist(token: str, limit: int = 50) -> list[dict]:
    """Wishlist beers, normalized to the same shape as search_beers() results."""
    result = await _call_tool("get_my_wishlist", {"limit": limit}, token)
    items = (result.get("beers") or {}).get("items", []) if isinstance(result, dict) else []
    out = []
    for item in items:
        b = item.get("beer", {}) or {}
        br = item.get("brewery", {}) or {}
        out.append({
            "bid": b.get("bid"),
            "beerName": b.get("beer_name"),
            "brewery": {"name": br.get("brewery_name")},
            "style": b.get("beer_style"),
            "abv": b.get("beer_abv"),
            "ibu": b.get("beer_ibu"),
            "globalRating": b.get("rating_score"),
            "ratingCount": b.get("rating_count"),
            "labelUrl": b.get("beer_label"),
        })
    return out


async def get_user_checkins(
    token: str, username: str, limit: int = 25,
    max_id: int | None = None, min_id: int | None = None,
) -> dict:
    """Raw check-in feed page, newest-first (or older than max_id when
    paging backwards, or newer than min_id when polling forward for what's
    new - see auto_toast.py). Each item may or may not carry a "venue" key;
    when present, venue.foursquare.foursquare_id is what venue_index.py
    indexes, and venue.location.venue_country is what auto_toast.py checks
    against its country exclusion list. Always returns the same normalized
    envelope: {"pagination": {"max_id", ...}, "checkins": {"count", "items": [...]}}.
    Costs Untappd quota per call, like get_user_beers.

    Confirmed live this session: when called WITH minId, the underlying MCP
    tool returns a *differently shaped* envelope - {"count", "items",
    "pagination"} flat, not nested under "checkins" - than when called
    without it. Every caller here was written against the nested shape, so
    a minId call silently looked empty (`.get("checkins", {})` found
    nothing) even when the API genuinely had new check-ins - this is
    exactly what caused auto_toast.py to see 0 new check-ins for accounts
    that had really posted several. Normalized here once so no caller has
    to special-case it."""
    arguments: dict = {"username": username, "limit": limit}
    if max_id is not None:
        arguments["maxId"] = max_id
    if min_id is not None:
        arguments["minId"] = min_id
    result = await _call_tool("get_user_checkins", arguments, token)
    if isinstance(result, dict) and "checkins" not in result and "items" in result:
        result = {
            "pagination": result.get("pagination"),
            "checkins": {"count": result.get("count"), "items": result.get("items", [])},
        }
    return result


async def get_my_friend_feed(
    token: str, limit: int = 50,
    max_id: int | None = None, min_id: int | None = None,
) -> dict:
    """The connected account's combined friend feed (Untappd's own
    "checkin/recent" - everyone the account follows, newest-first in one
    call) - added to the MCP server specifically so auto_toast.py wouldn't
    need one get_user_checkins call per watched target. Confirmed live:
    unlike get_user_checkins, this one keeps the same nested envelope shape
    - {"pagination": {...}, "checkins": {"count", "items": [...]}} -
    whether or not minId/maxId are passed, and minId genuinely filters
    (get_user_checkins's minId was observed to just return the newest
    `limit` regardless). maxId pages backward (older than this checkin_id),
    minId returns only newer than this checkin_id. Costs quota per call."""
    arguments: dict = {"limit": limit}
    if max_id is not None:
        arguments["maxId"] = max_id
    if min_id is not None:
        arguments["minId"] = min_id
    return await _call_tool("get_my_friend_feed", arguments, token)


async def comment_checkin(token: str, checkin_id: int, comment: str) -> dict:
    """Posts a comment on a check-in (own or someone else's) as the
    connected account. Untappd comments have no native threading, so a
    reply-to-a-specific-commenter convention is just text - see
    comment_watch.py's reply flow, which prefixes with "@username, "."""
    return await _call_tool("comment_checkin", {"checkinId": checkin_id, "comment": comment}, token)


async def get_my_recent_venues(token: str, limit: int = 20) -> list[dict]:
    result = await _call_tool("get_my_recent_venues", {"limit": limit}, token)
    return result.get("venues", []) if isinstance(result, dict) else []


async def get_user_friends(token: str, username: str | None = None, limit: int = 25, offset: int = 0) -> dict:
    """Raw envelope: {"found": total_count, "count": page_count, "items":
    [{"user": {"user_name", "first_name", "last_name", "user_avatar", ...}},
    ...]}. Omit username for the connected account's own friends (there's
    no single-friend lookup - page with offset for more than one page's
    worth). Costs quota per call, like get_user_beers."""
    arguments: dict = {"limit": limit, "offset": offset}
    if username:
        arguments["username"] = username
    return await _call_tool("get_user_friends", arguments, token)


async def get_untappd_api_usage(token: str) -> dict:
    return await _call_tool("get_untappd_api_usage", {}, token)


async def check_in(
    token: str,
    beer_id: int,
    rating: float = 0,
    shout: str = "",
    foursquare_id: str | None = None,
    geolat: float | None = None,
    geolng: float | None = None,
) -> dict:
    tz_name, gmt_offset = _current_tz_and_offset()
    arguments = {
        "beerId": beer_id,
        "rating": rating,
        "shout": shout,
        "timezone": tz_name,
        "gmtOffset": gmt_offset,
    }
    if foursquare_id:
        arguments["foursquareId"] = foursquare_id
        arguments["geolat"] = geolat
        arguments["geolng"] = geolng
    return await _call_tool("check_in", arguments, token)


async def toast_checkin(token: str, checkin_id: int) -> dict:
    """Toggles a toast on someone's check-in - adds one if not already
    toasted, removes it if it was. Result carries `action`/`nowToasted`."""
    return await _call_tool("toast_checkin", {"checkinId": checkin_id}, token)
