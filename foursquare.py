"""Thin async client for Foursquare's Places API (nearby venue search).

Independent of untappd_mcp/user_tokens - a venue found here is just handed
straight to Untappd's check_in as foursquareId (confirmed same ID space this
session: a real Untappd-known venue resolved identically via this API's
GET /places/{id}). Auth is the user's own FOURSQUARE_API_KEY, not shared
with anyone else the way the Untappd MCP quota is.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://places-api.foursquare.com"
_API_VERSION = "2025-06-17"

_client: httpx.AsyncClient | None = None


class FoursquareError(Exception):
    """Raised when the Foursquare API itself returns an error."""


class FoursquareRateLimited(FoursquareError):
    """Raised on HTTP 429. Callers must not retry automatically."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def search_nearby(
    lat: float | None = None, lng: float | None = None,
    query: str | None = None, radius: int = 500, limit: int = 20,
) -> list[dict]:
    """Places near (lat, lng), optionally filtered by a text query (venue
    name/brand). Either lat/lng or query (or both) should be given - with
    only a query, Foursquare falls back to its own (coarser, IP-based) geo
    bias instead of erroring. Returns
    [{"foursquareId", "name", "lat", "lng", "category", "categories"}, ...] -
    "categories" carries every category/subcategory name Foursquare returned
    (not just the primary one), for matching against badge_venue_categories.json
    in webapp_server.py's "badge only" filter - a venue can have a secondary
    category that's the one a badge actually cares about."""
    api_key = os.environ.get("FOURSQUARE_API_KEY", "")
    if not api_key:
        raise FoursquareError("FOURSQUARE_API_KEY not configured")

    params: dict = {"limit": limit}
    if lat is not None and lng is not None:
        params["ll"] = f"{lat},{lng}"
        params["radius"] = radius
    if query:
        params["query"] = query

    client = _get_client()
    try:
        resp = await client.get(
            f"{_BASE_URL}/places/search",
            params=params,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Places-Api-Version": _API_VERSION,
                "accept": "application/json",
            },
        )
    except httpx.HTTPError as e:
        raise FoursquareError(f"Foursquare request failed: {e}") from e

    if resp.status_code == 429:
        raise FoursquareRateLimited("Foursquare Places API returned HTTP 429")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise FoursquareError(f"Foursquare search failed: {e}") from e

    data = resp.json()
    out = []
    for place in data.get("results", []):
        fsq_id = place.get("fsq_place_id")
        if not fsq_id:
            continue
        categories = place.get("categories") or []
        # Foursquare returns categories most-specific-first; short_name reads
        # better in a compact list row than the full "name" (e.g. "Pub" vs
        # "Bar" full names can be long, short_name stays terse).
        category = (categories[0].get("short_name") or categories[0].get("name")) if categories else None
        # Both name and short_name go in, deduped - badge category text
        # (scraped from Untappd's own badge descriptions) sometimes matches
        # one form better than the other.
        all_names = []
        for c in categories:
            for n in (c.get("name"), c.get("short_name")):
                if n and n not in all_names:
                    all_names.append(n)
        out.append({
            "foursquareId": fsq_id,
            "name": place.get("name"),
            "lat": place.get("latitude"),
            "lng": place.get("longitude"),
            "category": category,
            "categories": all_names,
        })
    return out
