"""Daily crawl of the beer shops the Untappd Lens userscript supports, so the
lens outcome log (lens_log.py) fills with every product those shops list -
not only the pages someone happens to open - and recognition problems show
up in /api/lens/report without anyone browsing. Plain HTTP + HTML/JSON
parsing, no browser and no LLM: zero tokens.

Each adapter turns a shop into [{"brewery", "name", "shop"}] and mirrors the
userscript's own extraction for that shop (userscripts/untappd-lens.user.js,
ADAPTERS) - same title cleanup, same bundle/merch skipping - so the log sees
the exact strings the lens would send. Fetching is injected (`get_text` /
`get_json`) so the parsers are testable against saved fixtures, and every
shop is crawled independently: one shop failing or changing its markup never
stops the others (the error and an item count of 0 land in the crawl state,
which /api/lens/report shows).

Deliberately polite: default httpx User-Agent (a spoofed browser UA gets a
Cloudflare challenge - see venue_scrape.py), a pause between requests, one
run a day, page caps.

onemorebeer.pl renders its listing client-side, but the page itself calls a
public JSON API (api-prod.onecommerce.shop, products/search/items) with the
site's own tenant key in a "one-tenant" header - the same anonymous call every
visitor's browser makes - so it is crawled through that, 100 products a page.
"""

import asyncio
import json
import os
import re
import time

import httpx
from bs4 import BeautifulSoup

PAGE_GAP_SECONDS = float(os.environ.get("SHOP_CRAWL_PAGE_GAP_SECONDS", "2"))
ONTAP_VENUES = [v.strip() for v in os.environ.get("SHOP_CRAWL_ONTAP_VENUES", "pinta-wroclaw").split(",") if v.strip()]
MAX_PAGES = {"piwnemosty": 80, "hoptimaal": 20, "onemorebeer": 40}
ONEMOREBEER_TENANT = os.environ.get("SHOP_CRAWL_ONEMOREBEER_TENANT", "pinta")
ONEMOREBEER_API = (
    "https://api-prod.onecommerce.shop/api/v1/catalog/app/auth-optional/products/search/items"
    "?category=Piwa&sortCriteria=RANK_DESC&q=%2A&pageSize=100&pageNumber={page}"
)

_BUNDLE_RE = re.compile(
    r"\b(fan box|zestaw|gift box|mixed pack|box of \d+|set|assortment|bundle|pakket|proefpakket|cadeau|geschenk)\b", re.I
)
_state_path: str | None = None


class ShopError(Exception):
    pass


def init(data_dir: str) -> None:
    global _state_path
    _state_path = os.path.join(data_dir, "shop_crawl_state.json")


def load_state() -> dict:
    if not _state_path or not os.path.exists(_state_path):
        return {}
    try:
        with open(_state_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    tmp = _state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _state_path)


# ---- parsers (pure) ---------------------------------------------------------

def _clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_piwnemosty(html: str) -> tuple[list[dict], int]:
    """(items, last_counter): "Brewery: Beer Name - 500 ml can" per
    a.product__name, same cleanup as the userscript; last_counter is the
    highest ?counter=N in the pager (pages are counter=0..N)."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.product"):
        el = card.select_one("a.product__name")
        if el is None:
            continue
        raw = _clean_spaces(el.get_text(" "))
        if not raw or _BUNDLE_RE.search(raw):
            continue
        text = re.sub(r"\b\d+\s*x\s*[\d.,]+\s*m?l\b\s*(?:can|bottle|keg)?", " ", raw, flags=re.I)
        text = re.sub(r"\s*-\s*[\d.,]+\s*m?l\b.*$", "", text, flags=re.I)
        text = _clean_spaces(text)
        brewery, sep, name = text.partition(":")
        brewery, name = (brewery.strip(), name.strip()) if sep else ("", text)
        if name:
            items.append({"brewery": brewery, "name": name})
    counters = [int(m) for m in re.findall(r"[?&]counter=(\d+)", html)]
    return items, max(counters, default=0)


def parse_ontap(html: str) -> list[dict]:
    """A venue taplist: brewery in b.brewery, the beer name a bare text node
    between the first two <br> tags of h4.cml_shadow > span."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.panel.panel-default"):
        span = card.select_one("h4.cml_shadow > span")
        if span is None:
            continue
        brewery_el = span.select_one("b.brewery")
        brewery = _clean_spaces(brewery_el.get_text(" ")) if brewery_el else ""
        children = list(span.children)
        br_idx = [i for i, n in enumerate(children) if getattr(n, "name", None) == "br"]
        if not br_idx:
            continue
        end = br_idx[1] if len(br_idx) > 1 else len(children)
        name = _clean_spaces(" ".join(str(n) for n in children[br_idx[0] + 1:end] if isinstance(n, str)))
        name = re.sub(r"\s*[\d.,]+\s*°\s*$", "", name).strip()  # leaked Plato degrees
        if name:
            items.append({"brewery": brewery, "name": name})
    return items


# A venue taplist also pours wine and mixed drinks, which Untappd's beer
# catalog doesn't have - they'd only ever show up as "no results" and bury the
# real misses in the report. Narrow on purpose: whole phrases that are only ever
# a drink, never a word a beer name could plausibly contain ("whisky" alone
# stays - barrel-aged beers use it).
_NON_BEER_RE = re.compile(
    r"\b(?:frizzante|prosecco|glera|aperol|spritz|mojito|margarita|negroni|cuba\s+libre|whisk(?:e)?y\s+z\s+col\w*)\b",
    re.IGNORECASE,
)


def is_non_beer(item: dict) -> bool:
    return bool(_NON_BEER_RE.search(f"{item.get('brewery', '')} {item.get('name', '')}"))


# Merchandise that slipped past a shop's own product-type filter - found in the
# lens log: WRCLW's "Pszeniczny T-Shirt" matched the beer "WRCLW Pszeniczny".
_MERCH_RE = re.compile(
    r"\b(?:t-?shirt|koszulk\w*|bluz\w*|hoodie|czapk\w*|kubek|szklank\w*|kufel|tumbler|plakat|naklejk\w*|sticker|bidon)\b",
    re.IGNORECASE,
)


def is_not_a_beer(item: dict) -> bool:
    """Wine/cocktail taplist entries or merchandise - nothing to look up."""
    return is_non_beer(item) or bool(_MERCH_RE.search(item.get("name", "")))


def parse_hopincraftbier(html: str) -> list[dict]:
    """"Brewery - Beer name" titles. Titles without that separator are shop
    items that aren't beers (gift card, can clips) - skipped here, unlike the
    userscript, so they don't fill the log with "not found" noise."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.grid-product"):
        el = card.select_one(".grid-product__title-inner")
        if el is None:
            continue
        text = _clean_spaces(el.get_text(" "))
        if not text or _BUNDLE_RE.search(text) or " - " not in text:
            continue
        brewery, _, name = text.partition(" - ")
        if name.strip():
            items.append({"brewery": brewery.strip(), "name": name.strip()})
    return items


def parse_hoptimaal(payload: dict) -> list[dict]:
    """Shopify products.json: vendor = brewery, title = name; merch skipped."""
    items = []
    for p in payload.get("products") or []:
        title = _clean_spaces(p.get("title") or "")
        if not title or (p.get("product_type") or "").strip().lower() == "merch" or _BUNDLE_RE.search(title):
            continue
        items.append({"brewery": _clean_spaces(p.get("vendor") or ""), "name": title})
    return items


def parse_onemorebeer(payload: dict) -> list[dict]:
    """products/search/items: `name` is the shop title (same cleanup as the
    userscript's onemorebeer adapter), the brewery is the "Producent"
    characteristic (falling back to the manufacturer record)."""
    items = []
    for p in payload.get("items") or []:
        text = p.get("name") or ""
        if not text.strip() or _BUNDLE_RE.search(text):
            continue
        text = re.sub(r"\b(BUT\.?|BUTELKA|PUSZKA|KEG)\s*[\d.,]+\s*L\b", " ", text, flags=re.I)  # "BUT. 0,5 L", "PUSZKA 0,44 L", "KEG 30 L"
        text = re.sub(r"\bKAUCJA\b", " ", text, flags=re.I)  # bottle/can deposit note
        text = re.sub(r"\b(?:B\.?)?ZW\b\.?", " ", text, flags=re.I)  # returnable/non-returnable bottle marker
        text = re.sub(r"\(\s*gazetka\s*\)", " ", text, flags=re.I)  # "featured in this week's flyer"
        text = re.sub(r"\bdata\s+wa[żz]no[śs]ci\s+\d{1,2}[./]\d{1,2}[./]\d{2,4}\b", " ", text, flags=re.I)
        text = re.sub(r"[\d.,]+\s*°", " ", text)  # leaked Plato degrees
        text = _clean_spaces(text)
        if not text:
            continue
        brewery = next(
            (_clean_spaces(c.get("value") or "") for c in p.get("characteristics") or []
             if (c.get("characteristicName") or "").startswith("Producent")),
            "",
        ) or _clean_spaces((p.get("manufacturer") or {}).get("name") or "")
        items.append({"brewery": brewery, "name": text})
    return items


# ---- crawl (network injected) -----------------------------------------------

async def _crawl_piwnemosty(get_text, sleep, gap) -> tuple[list[dict], int]:
    base = "https://www.piwnemosty.pl/pol_m_PIWO-KRAFTOWE-100.html"
    items, last = parse_piwnemosty(await get_text(base))
    pages = 1
    for counter in range(1, min(last, MAX_PAGES["piwnemosty"] - 1) + 1):
        await sleep(gap)
        page_items, _ = parse_piwnemosty(await get_text(f"{base}?counter={counter}"))
        pages += 1
        if not page_items:
            break
        items.extend(page_items)
    return items, pages


async def _crawl_hoptimaal(get_json, sleep, gap) -> tuple[list[dict], int]:
    items, pages = [], 0
    for page in range(1, MAX_PAGES["hoptimaal"] + 1):
        if page > 1:
            await sleep(gap)
        payload = await get_json(f"https://hoptimaal.com/products.json?limit=250&page={page}")
        pages += 1
        if not payload.get("products"):
            break
        items.extend(parse_hoptimaal(payload))
    return items, pages


async def _crawl_onemorebeer(get_json, sleep, gap) -> tuple[list[dict], int]:
    headers = {"one-tenant": ONEMOREBEER_TENANT}
    items, pages = [], 0
    for page in range(1, MAX_PAGES["onemorebeer"] + 1):
        if page > 1:
            await sleep(gap)
        payload = await get_json(ONEMOREBEER_API.format(page=page), headers)
        pages += 1
        if not payload.get("items"):
            break
        items.extend(parse_onemorebeer(payload))
        if page >= (payload.get("totalPages") or 0):
            break
    return items, pages


async def _crawl_hopincraftbier(get_text, sleep, gap) -> tuple[list[dict], int]:
    items = parse_hopincraftbier(await get_text("https://hopincraftbier.be/products"))
    await sleep(gap)
    items.extend(parse_hopincraftbier(await get_text("https://hopincraftbier.be/products/verwacht")))
    return items, 2


async def _crawl_ontap(get_text, sleep, gap) -> tuple[list[dict], int]:
    items = []
    for i, venue in enumerate(ONTAP_VENUES):
        if i:
            await sleep(gap)
        items.extend(it for it in parse_ontap(await get_text(f"https://{venue}.ontap.pl/")) if not is_non_beer(it))
    return items, len(ONTAP_VENUES)


async def crawl_shops(get_text, get_json, sleep=asyncio.sleep, gap: float = PAGE_GAP_SECONDS) -> dict:
    """{shop: {"items": [...], "pages": n, "error": str | None}} - each shop
    in isolation. `items` carry the shop name under "shop"."""
    jobs = {
        "piwnemosty": lambda: _crawl_piwnemosty(get_text, sleep, gap),
        "hoptimaal": lambda: _crawl_hoptimaal(get_json, sleep, gap),
        "hopincraftbier": lambda: _crawl_hopincraftbier(get_text, sleep, gap),
        "ontap": lambda: _crawl_ontap(get_text, sleep, gap),
        "onemorebeer": lambda: _crawl_onemorebeer(get_json, sleep, gap),
    }
    out = {}
    for shop, job in jobs.items():
        try:
            items, pages = await job()
            out[shop] = {"items": [{**it, "shop": shop} for it in items], "pages": pages, "error": None}
        except Exception as e:  # noqa: BLE001 - a broken shop must never stop the others
            out[shop] = {"items": [], "pages": 0, "error": f"{type(e).__name__}: {e}"[:200]}
        await sleep(gap)
    return out


def make_http_getters(client: httpx.AsyncClient):
    async def get_text(url: str) -> str:
        r = await client.get(url)
        if r.status_code != 200:
            raise ShopError(f"HTTP {r.status_code} for {url}")
        return r.content.decode("utf-8", "replace")

    async def get_json(url: str, headers: dict | None = None) -> dict:
        r = await client.get(url, headers=headers)
        if r.status_code != 200:
            raise ShopError(f"HTTP {r.status_code} for {url}")
        return r.json()

    return get_text, get_json


def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = (it["brewery"].lower(), it["name"].lower())
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def summarize(crawl: dict) -> dict:
    now = int(time.time())
    return {shop: {"items": len(r["items"]), "pages": r["pages"], "error": r["error"], "at": now} for shop, r in crawl.items()}
