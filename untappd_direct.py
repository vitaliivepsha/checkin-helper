"""Thin async client for the REAL Untappd API v4 (api.untappd.com) - a
SEPARATE credential/quota pool from untappd_mcp.py's remote MCP proxy, used
ONLY as a fallback for the recognized owner (UNTAPPD_API_ACCESS_TOKEN) when
the owner's own MCP-token quota is tapped out - see webapp_server.py's
_pick_untappd_backend. Deliberately NOT offered to regular connected users:
getting a real Untappd API access_token requires an approved Untappd API
application, which Untappd has not been granting to new developers for
years, so the MCP proxy remains the only realistic path for everyone else.

Mirrors untappd_mcp.py's function names/signatures for exactly the
functions the background sync loops need (get_user_beers, get_user_checkins,
get_my_friend_feed, comment_checkin, toast_checkin) plus get_my_profile
(handy for a one-off "does this token even work" check) - the real API's
response shapes for these particular calls already match what untappd_mcp.py
returns (untappd_mcp's own docstrings/field names - beer_name, beer_style,
rating_score, brewery_name, checkin_id, etc. - are literally the real
Untappd API's own field names, and get_user_checkins' raw per-item shape was
independently confirmed live this project against the real API's documented
fields), so callers can treat this module as a drop-in swap for untappd_mcp
with no reshaping needed. Raises the SAME exception classes as untappd_mcp
(imported from there, not redefined) so existing
`except untappd_mcp.UntappdRateLimited` call sites keep working unchanged
regardless of which client actually made a given call.

get_venue_checkins - the actual reason this token exists (see README's
"Новинки"/festival_watch limitation note: get_my_friend_feed structurally
can only ever see FRIENDS, never "anyone at this venue") - is intentionally
NOT implemented here yet. That's a separate feature (its own independent
poll loop, a Foursquare-id -> Untappd venue_id resolution step, and a UI
change to how a watch point is set), not a same-shape fallback for the
existing loops - see README's staged plan.
"""

import logging
import time

import httpx

from untappd_mcp import UntappdMalformedResponse, UntappdMCPError, UntappdRateLimited

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.untappd.com/v4"
_client: httpx.AsyncClient | None = None

# Rate-limit headroom, tracked from the real X-Ratelimit-* response headers
# on every call this module makes. Unlike untappd_mcp's own MCP server
# (which exposes a dedicated get_untappd_api_usage tool backed by its own
# server-side bookkeeping), the real Untappd API has no "how much quota do I
# have left" endpoint at all - these per-response headers are the only
# source of truth (see the official API docs' rate-limiting section).
# get_api_usage()'s return shape is deliberately identical to
# untappd_mcp.get_untappd_api_usage()'s own ({"lastSeen": {"remaining",
# "ageSeconds"}}) so webapp_server.py's _quota_allows() gates either source
# with the exact same function, unchanged.
_last_remaining: int | None = None
_last_limit: int | None = None
_last_seen_at: float | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


def get_api_usage() -> dict:
    """Synchronous and free (no network call, just reads module state) -
    None/absent until this module has made at least one real call, which
    _quota_allows() already treats as "allow" (no info yet != no quota).
    "limit" is captured too (X-Ratelimit-Limit, not just -Remaining) even
    though _quota_allows itself never reads it - it's what actually
    answers "what IS this token's real per-hour ceiling", which turned out
    NOT to be inferable from a single "remaining" reading alone (a
    partially-spent token looks identical to a smaller-limit fresh one) -
    confirmed live this session: a "remaining: 25" first reading was
    wrongly assumed to mean a 25/hour limit, when the real limit (this
    same field) was 100 - the token just already had 75 calls spent on it
    from earlier that hour. shape matches untappd_mcp.
    get_untappd_api_usage()'s own lastSeen.limit field for the same
    reason get_api_usage's other fields do."""
    if _last_seen_at is None:
        return {"lastSeen": {"remaining": None, "limit": None}}
    return {
        "lastSeen": {
            "remaining": _last_remaining, "limit": _last_limit,
            "ageSeconds": time.time() - _last_seen_at,
        },
    }


async def _call_api(method: str, path: str, token: str, params: dict | None = None) -> dict:
    global _last_remaining, _last_limit, _last_seen_at
    if not token:
        raise UntappdMCPError("no Untappd API access token provided")

    query = dict(params or {})
    query["access_token"] = token
    client = _get_client()
    url = f"{_BASE_URL}/{path}"
    try:
        resp = await client.request(method, url, params=query)
    except httpx.HTTPError as e:
        raise UntappdMCPError(f"Untappd API request failed: {e}") from e

    # Captured even on an error response - a 429/rate-limited response still
    # carries the (now-zero) remaining count, which is itself useful signal.
    remaining_header = resp.headers.get("X-Ratelimit-Remaining")
    limit_header = resp.headers.get("X-Ratelimit-Limit")
    if remaining_header is not None:
        try:
            _last_remaining = int(remaining_header)
            _last_seen_at = time.time()
        except ValueError:
            pass
    if limit_header is not None:
        try:
            _last_limit = int(limit_header)
        except ValueError:
            pass

    if resp.status_code == 429:
        raise UntappdRateLimited("Untappd API returned HTTP 429")

    try:
        body = resp.json()
    except ValueError as e:
        raise UntappdMalformedResponse(f"Malformed Untappd API response JSON: {e}") from e

    meta = body.get("meta") or {}
    # code 200 is the only success value the real API ever returns - same
    # tolerant "rate"/"429" substring check untappd_mcp._call_tool already
    # uses for its own error messages, so a rate-limit error phrased either
    # way (real HTTP 429 above, or a 200-wrapped meta error here) is
    # classified the same way for callers.
    if meta.get("code") != 200:
        message = str(meta.get("error_detail") or meta.get("developer_friendly") or meta)
        if "429" in message or "rate" in message.lower():
            raise UntappdRateLimited(message)
        raise UntappdMCPError(message)

    return body.get("response") or {}


async def get_my_profile(token: str) -> dict:
    return await _call_api("GET", "user/info", token)


async def check_i_had_beer(token: str, beer_id: int) -> dict:
    """Whether the token's OWN account has ever checked in this beer -
    unlike get_user_badges/get_venue_checkins, this is NOT a public,
    any-username lookup: beer/info's per-viewer fields (auth_rating,
    stats.user_count) always reflect whoever's access_token made the call,
    with no way to ask "for username X" the way those other endpoints
    allow. Callers MUST NOT use this for any connected user other than
    DIRECT_TOKEN's own account (the recognized owner) - see
    webapp_server.py's _get_had_it, which only reaches for this fallback
    when the caller IS the owner.

    Confirmed live (comparing against untappd_mcp.check_i_had_beer's own
    ground truth for the same beer_id): stats.user_count is the reliable
    signal (0 vs >=1, an actual per-account checkin count, no ambiguity) -
    auth_rating alone would be ambiguous (0 could mean "never had" OR "had
    it, rated it 0 stars"), so hadIt is decided from user_count, never from
    auth_rating directly. Returns the same shape untappd_mcp.
    check_i_had_beer's own callers already expect: {"hadIt",
    "userRating", "userCheckinCount"}."""
    result = await _call_api("GET", f"beer/info/{beer_id}", token)
    beer = result.get("beer") or {}
    stats = beer.get("stats") or {}
    checkin_count = stats.get("user_count") or 0
    had_it = checkin_count > 0
    return {
        "hadIt": had_it,
        "userRating": beer.get("auth_rating") if had_it else None,
        "userCheckinCount": checkin_count,
    }


async def get_user_badges(token: str, username: str, limit: int = 50, offset: int = 0) -> dict:
    """Full, always-current list of every badge this user has actually
    earned - confirmed live to work for ANY username via a single token
    (public, same as get_venue_checkins - not scoped to the token's own
    account), and confirmed to be a MUCH better ground truth source than
    badge_index.py's original design (scavenging a badge_name/user_badge_id
    pair from whichever check-ins happen to carry one in their own "badges"
    array - necessarily incomplete/laggy, since only the exact check-in
    that triggered a level-up ever carries that entry).

    Returns the raw envelope: {"type", "sort", "count", "items": [...]}
    (NOT wrapped in a nested key - unlike most other endpoints here, this
    one's "response" IS the list envelope directly). Each item already
    carries badge_name with a "(Level N)" suffix baked in for repeating
    badges (e.g. "Ales for ALS (2026) (Level 24)") and a current
    user_badge_id - both directly compatible with badge_index.record()'s
    existing parsing with zero changes needed there. `count` returned may
    be less than `limit` even mid-list on some pages (observed live) - the
    real end-of-list signal callers should use is `count < limit`, same
    convention as every other paginated endpoint here."""
    return await _call_api("GET", f"user/badges/{username}", token, {"limit": limit, "offset": offset})


async def get_user_beers(
    token: str,
    username: str,
    sort: str = "date",
    limit: int = 50,
    offset: int = 0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    params: dict = {"sort": sort, "limit": limit, "offset": offset}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return await _call_api("GET", f"user/beers/{username}", token, params)


async def get_user_checkins(
    token: str, username: str, limit: int = 25,
    max_id: int | None = None, min_id: int | None = None,
) -> dict:
    params: dict = {"limit": limit}
    if max_id is not None:
        params["max_id"] = max_id
    if min_id is not None:
        params["min_id"] = min_id
    result = await _call_api("GET", f"user/checkins/{username}", token, params)
    # Same envelope-shape normalization untappd_mcp.get_user_checkins does
    # for its own occasional flat-shaped response - kept here too so this
    # module is a true drop-in regardless of which shape the real API
    # happens to hand back.
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
    params: dict = {"limit": limit}
    if max_id is not None:
        params["max_id"] = max_id
    if min_id is not None:
        params["min_id"] = min_id
    return await _call_api("GET", "checkin/recent", token, params)


async def comment_checkin(token: str, checkin_id: int, comment: str) -> dict:
    return await _call_api("POST", f"checkin/addcomment/{checkin_id}", token, {"comment": comment})


async def toast_checkin(token: str, checkin_id: int) -> dict:
    return await _call_api("POST", f"checkin/toast/{checkin_id}", token)


async def lookup_venue_by_foursquare(token: str, foursquare_id: str) -> dict | None:
    """Translates a Foursquare venue id into Untappd's own numeric
    venue_id, needed for get_venue_checkins below (that endpoint takes an
    Untappd venue_id, not a Foursquare one). Untappd's own docs describe the
    expected id as a "Foursquare venue v2 ID"; this project's own
    foursquare.py talks to Foursquare's newer Places API instead (a
    different id space in general) - confirmed LIVE this session that
    Untappd still resolves it correctly anyway (tested against several real
    foursquareIds already proven to work for check-in venue attribution via
    foursquare.py, same as that module's own docstring already claims for
    check_in's separate foursquareId code path).

    Real shape (confirmed live, NOT what the raw endpoint name might
    suggest): {"venue": {"count", "items": [{"venue_id", "venue_name",
    "foursquare_id", "last_updated"}]}} - a search-shaped envelope even
    though it only ever returns 0 or 1 result for one input id. Returns
    {"venueId", "venueName"} directly, or None if this foursquare_id has no
    known Untappd venue yet - a real, unremarkable case (not every
    Foursquare place has ever been checked into on Untappd), confirmed live
    to come back not as an empty items[] list but as an actual API error
    ("There is no Untappd venue match for ...") - caught and normalized to
    None here so callers don't need their own special-case for it."""
    try:
        result = await _call_api("GET", f"venue/foursquare_lookup/{foursquare_id}", token)
    except UntappdMCPError as e:
        if "no untappd venue match" in str(e).lower():
            return None
        raise
    items = (result.get("venue") or {}).get("items") or []
    if not items:
        return None
    venue = items[0]
    venue_id = venue.get("venue_id")
    if venue_id is None:
        return None
    return {"venueId": venue_id, "venueName": venue.get("venue_name")}


async def get_venue_checkins(
    token: str, venue_id: int, limit: int = 25,
    max_id: int | None = None, min_id: int | None = None,
) -> dict:
    """Public check-in feed for one specific venue - NOT limited to the
    calling account's friends, unlike get_my_friend_feed/checkin/recent
    (see README's festival_watch limitation note - this is the whole reason
    untappd_direct.py exists). Same envelope shape as get_user_checkins."""
    params: dict = {"limit": limit}
    if max_id is not None:
        params["max_id"] = max_id
    if min_id is not None:
        params["min_id"] = min_id
    result = await _call_api("GET", f"venue/checkins/{venue_id}", token, params)
    if isinstance(result, dict) and "checkins" not in result and "items" in result:
        result = {
            "pagination": result.get("pagination"),
            "checkins": {"count": result.get("count"), "items": result.get("items", [])},
        }
    return result
