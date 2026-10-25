"""Replays the lens's logged products through the CURRENT matcher and shows what a rule change would do -
run it before (and after) touching beer_match.py:

    python lens_regress.py                  # against the production report
    python lens_regress.py --base URL       # another origin (default: LENS_REGRESS_BASE or the VM)
    python lens_regress.py --limit 500      # how many rows of each report list to replay

It takes /api/lens/report (X-Lens-Token = LENS_API_TOKEN from .env): the unmatched, suspicious and
changed lists plus every rejection, and re-resolves each product with the matcher in this checkout
(search is the quota-free public index; the token is UNTAPPD_MCP_TOKEN). Then it prints
  - products that were unmatched and now match (check them by eye),
  - products whose matched beer changed or that stopped matching (possible regressions),
  - products that became "probable",
and, as the hard rule, any product where the matcher still returns a beer the user REJECTED (exit
code 1). Rejections are fed to the matcher the way the server does, so this also checks that the
rejection mechanism holds under the current rules."""

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()

import beer_match  # noqa: E402

DEFAULT_BASE = os.environ.get("LENS_REGRESS_BASE", "https://35-211-15-162.sslip.io")
CONCURRENCY = 5


def fetch_report(base: str, limit: int) -> dict:
    request = urllib.request.Request(
        f"{base}/api/lens/report?limit={limit}", headers={"X-Lens-Token": os.environ["LENS_API_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def rows_from(report: dict) -> list[dict]:
    """Every distinct (brewery, name) in the report's lists, with what the log says it was."""
    rows: dict[tuple[str, str], dict] = {}
    for kind in ("unmatched", "suspicious", "changed"):
        for e in report.get(kind, []):
            key = (e["queryBrewery"], e["queryName"])
            rows.setdefault(key, {"brewery": e["queryBrewery"], "name": e["queryName"], "was": e.get("bid"),
                                  "wasOutcome": e.get("outcome"), "wasName": e.get("matchedName")})
    return list(rows.values())


async def replay(rows: list[dict], rejected_by_text: dict, token: str) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict) -> dict:
        rejected = frozenset(rejected_by_text.get((row["brewery"], row["name"]), ()))
        async with sem:
            try:
                identity = await beer_match._resolve_identity(token, row["name"], row["brewery"], rejected)
            except Exception as exc:  # a failed search must not hide the others
                return {**row, "error": str(exc)}
        return {**row, "now": identity.get("bid") if identity["matched"] else None,
                "nowName": identity.get("name"), "via": identity.get("via"),
                "probable": (identity.get("probable") or {}).get("name"), "rejected": rejected}

    return await asyncio.gather(*(one(r) for r in rows))


def summarize(results: list[dict]) -> int:
    failed = [r for r in results if r.get("error")]
    now_matched = [r for r in results if r.get("now") and not r["was"]]
    changed = [r for r in results if r.get("now") and r["was"] and r["now"] != r["was"]]
    lost = [r for r in results if "now" in r and r["now"] is None and r["was"]]
    probable = [r for r in results if "now" in r and r["now"] is None and r.get("probable")]
    violations = [r for r in results if r.get("now") and r["now"] in r["rejected"]]

    def show(title: str, rows: list[dict], fmt) -> None:
        print(f"\n{title}: {len(rows)}")
        for r in rows[:40]:
            print("  ", fmt(r))

    print(f"replayed {len(results)} products ({len(failed)} search errors)")
    show("unmatched -> now matched (check them)", now_matched,
         lambda r: f"{r['brewery']} | {r['name']}  =>  {r['nowName']}  [{r['via']}]")
    show("matched beer CHANGED", changed,
         lambda r: f"{r['brewery']} | {r['name']}  was {r['wasName']}  =>  {r['nowName']}  [{r['via']}]")
    show("matched before, NOT matched now", lost, lambda r: f"{r['brewery']} | {r['name']}  was {r['wasName']}")
    show("now probable (🤔)", probable, lambda r: f"{r['brewery']} | {r['name']}  ~>  {r['probable']}")
    show("REJECTED beer still returned", violations, lambda r: f"{r['brewery']} | {r['name']}  =>  {r['nowName']}")
    return 1 if violations else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = fetch_report(args.base, args.limit)
    rejected_by_text = {(e["queryBrewery"], e["queryName"]): e["bids"] for e in report.get("rejections", [])}
    rows = rows_from(report)
    for text in rejected_by_text:   # a rejected product is replayed even if the lists no longer show it
        if not any((r["brewery"], r["name"]) == text for r in rows):
            rows.append({"brewery": text[0], "name": text[1], "was": None, "wasOutcome": None, "wasName": None})
    results = asyncio.run(replay(rows, rejected_by_text, os.environ["UNTAPPD_MCP_TOKEN"]))
    return summarize(results)


if __name__ == "__main__":
    sys.exit(main())
