"""Logged-out scrape of an Untappd venue's public "Recent Activity" page
(untappd.com/v/<slug>/<venue_id>/activity) - the latest ~25 check-ins there,
with zero API quota. Exists so festival_watch can cover MANY neighbouring
venues, which the 100/hour DIRECT_TOKEN pool (venue/checkins, see
untappd_direct.get_venue_checkins) can't afford.

Returns items shaped like the API's check-in items (just the fields
webapp_server._notify_festival_novelty reads), so the same notification
path serves both. brewery_id is always None - the page only links the
brewery by slug.

Fragile by nature (HTML, Cloudflare) - failures raise ScrapeBlocked/
ScrapeError and callers back off rather than retry hard. Any slug redirects
to the real one, so only the venue id is needed ("-" is used as a stand-in).
"""

import html
import re

import httpx
from bs4 import BeautifulSoup

_client: httpx.AsyncClient | None = None


class ScrapeError(Exception):
    pass


class ScrapeBlocked(ScrapeError):
    """Cloudflare challenge / 403 / 429 - back off, don't hammer."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Deliberately NO custom User-Agent: confirmed live that Cloudflare
        # challenges (403, cf-mitigated) a realistic Chrome UA sent with
        # httpx's own TLS fingerprint (a mismatch looks like a spoofed
        # browser), while httpx's default UA passes.
        _client = httpx.AsyncClient(follow_redirects=True, timeout=20)
    return _client


def _id_from_href(href: str | None, prefix: str) -> int | None:
    m = re.search(rf"^{prefix}/(?:[^/]+/)?(\d+)$", href or "")
    return int(m.group(1)) if m else None


def parse_activity(page_html: str) -> list[dict]:
    """Newest-first list of check-in items found on an activity page. Items
    it can't make sense of (no beer link, odd layout) are skipped rather
    than raising - one odd entry shouldn't blind the whole poll."""
    soup = BeautifulSoup(page_html, "html.parser")
    items = []
    for node in soup.select("#main-stream div.item[data-checkin-id]"):
        text = node.select_one("p.text")
        if text is None:
            continue
        try:
            checkin_id = int(node["data-checkin-id"])
        except (KeyError, ValueError):
            continue
        user_a = text.select_one("a.user")
        beer_a = next((a for a in text.find_all("a", href=True) if a["href"].startswith("/b/")), None)
        venue_a = next((a for a in text.find_all("a", href=True) if a["href"].startswith("/v/")), None)
        if user_a is None or beer_a is None:
            continue
        beer_id = _id_from_href(beer_a["href"], "/b")
        if beer_id is None:
            continue
        # Brewery = the link right after the beer's own (a plain "/<slug>"
        # href, no /b/ or /v/ or /user/ prefix).
        brewery_a = next(
            (a for a in beer_a.find_all_next("a", href=True, limit=3)
             if re.fullmatch(r"/[^/]+", a["href"]) and a["href"] != user_a["href"]),
            None,
        )
        items.append({
            "checkin_id": checkin_id,
            "user": {"user_name": user_a["href"].rsplit("/", 1)[-1]},
            "beer": {"bid": beer_id, "beer_name": html.unescape(beer_a.get_text(strip=True))},
            "brewery": {
                "brewery_id": None,
                "brewery_name": html.unescape(brewery_a.get_text(strip=True)) if brewery_a else None,
            },
            "venue": {
                "venue_id": _id_from_href(venue_a["href"], "/v") if venue_a else None,
                "venue_name": html.unescape(venue_a.get_text(strip=True)) if venue_a else None,
            },
        })
    return items


async def fetch_venue_activity(venue_id: int) -> list[dict]:
    try:
        r = await _get_client().get(f"https://untappd.com/v/-/{venue_id}/activity")
    except httpx.HTTPError as e:
        raise ScrapeError(f"request failed: {e}") from e
    body = r.content.decode("utf-8", "replace")
    if r.status_code in (403, 429, 503) or "Just a moment" in body[:5000]:
        raise ScrapeBlocked(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise ScrapeError(f"HTTP {r.status_code}")
    if 'id="main-stream"' not in body:
        raise ScrapeError("no activity stream in page (layout changed, or venue has none)")
    return parse_activity(body)
