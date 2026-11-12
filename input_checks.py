"""Cleaning of values that arrive in request bodies before they are stored (audit S2/S6).

The Mini App sends display fields back to the server (a beer's name, brewery, label picture) and the server
stores them for other people to see - a shared queue is rendered on every group member's phone. Nothing in a
body can be trusted to be what the app would have sent, so every stored field is cut to a type and a length
here, and a picture address must be a plain https URL. Lists that a client can grow have a cap (StoreFull)."""


class StoreFull(Exception):
    """A per-user / per-group list reached its cap."""


MAX_TEXT = 200
MAX_URL = 300
MAX_WISHLIST = 500
MAX_QUEUE_PER_GROUP = 500
MAX_PENDING = 200


def text(value, max_len: int = MAX_TEXT):
    """A string cut to `max_len` (None stays None); numbers are kept as numbers; anything else is dropped."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value.strip()[:max_len]
    return None


def https_url(value, max_len: int = MAX_URL):
    """The value when it is a plain https URL (no whitespace, quotes or angle brackets, bounded), else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.lower().startswith("https://") or len(value) > max_len:
        return None
    if any(c.isspace() or c in "\"'<>`\\" for c in value):
        return None
    return value


def beer_fields(beer: dict) -> dict:
    """The display fields every list stores for a beer, cleaned."""
    return {
        "beerId": beer.get("beerId") if isinstance(beer.get("beerId"), int) and not isinstance(beer.get("beerId"), bool) else None,
        "name": text(beer.get("name") if "name" in beer else beer.get("beerName")),
        "brewery": text(beer.get("brewery")),
        "style": text(beer.get("style")),
        "abv": text(beer.get("abv"), 20),
        "labelUrl": https_url(beer.get("labelUrl")),
    }
