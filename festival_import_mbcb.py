"""Builds a festival beer file (the {session: [beer, ...]} shape of
mbcc_beers.json, see festivals.json) from mbcb.jonpacker.com - the community
beer-list site for MBCB 2026, whose page loads everything from one public
/latest.json (beers with their Untappd id, brewery and session colour).

    python festival_import_mbcb.py            # writes mbcb_beers.json
    python festival_import_mbcb.py out.json   # or somewhere else

Re-run it whenever the site updates its list. The site has no floor plan, so
beers carry no `location` (the festival map stays empty until zones are
defined, same as the 1928-beer test festival).
"""

import json
import sys

import httpx

SOURCE_URL = "https://mbcb.jonpacker.com/latest.json"
DEFAULT_OUTPUT = "mbcb_beers.json"


def build_festival_beers(latest: dict) -> tuple[dict[str, list[dict]], list[dict]]:
    """(beers_by_session, skipped). Sessions keep the site's own colour keys in
    the order they first appear; a beer poured in several sessions is listed
    in each (the same real beer, one entry per session, which is how the app
    counts a session's progress); a row with no Untappd id can't be checked in
    and is returned in `skipped` instead of being dropped silently."""
    sessions: dict[str, list[dict]] = {}
    seen: dict[str, set[str]] = {}
    skipped: list[dict] = []
    for row in latest.get("beers") or []:
        session = row.get("session")
        name = (row.get("name") or "").strip()
        bid = row.get("ut_bid")
        if not session or not name:
            skipped.append(row)
            continue
        if not bid:
            skipped.append(row)
            continue
        beer_id = str(int(bid))
        if beer_id in seen.setdefault(session, set()):
            continue
        seen[session].add(beer_id)
        sessions.setdefault(session, []).append({
            "id": beer_id,
            "name": name,
            "brewery": (row.get("brewery") or "").strip(),
            "url": f"https://untappd.com/beer/{beer_id}",
        })
    return sessions, skipped


def main(argv: list[str]) -> int:
    output = argv[1] if len(argv) > 1 else DEFAULT_OUTPUT
    latest = httpx.get(SOURCE_URL, timeout=30, follow_redirects=True).raise_for_status().json()
    sessions, skipped = build_festival_beers(latest)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)
        f.write("\n")
    distinct = {b["id"] for beers in sessions.values() for b in beers}
    print(f"{output}: {sum(len(v) for v in sessions.values())} entries in {len(sessions)} sessions, {len(distinct)} distinct beers")
    for row in skipped:
        print(f"  skipped (no Untappd id): {row.get('brewery')} - {row.get('name')} [{row.get('session')}]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
