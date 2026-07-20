#!/usr/bin/env python3
"""
Pull data from any nafnid.arnastofnun.is Django REST API endpoint,
following pagination automatically, and save as JSON + CSV.

Endpoints discovered from the site's own API root
(https://nafnid.arnastofnun.is/django/vefur/api/pages/?site=1 returns
a JSON object listing them all):

    pages, front_sections, oleit, bleit, geoleit, textaleit, siur,
    uuid, ornefnaskrar, ornefni, ornefnaskrareinstaklings,
    einstaklingar, baeir, hreppar, sveitarfelog, syslur, greinar,
    nofn_islendinga, nofn_tolfr, abending

Usage:
    python pull_endpoint.py ornefni --out ornefni
    python pull_endpoint.py geoleit --out farm --params type=farm
    python pull_endpoint.py textaleit --out kirkja_search --params q=kirkja

Polite by design: single-threaded, sequential, with a delay between
pages. Treat any pulled data as a personal working copy for
reconciliation/research use only until terms are confirmed with
Árnastofnun's onomastics department.
"""

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import NAFNID_DATA_DIR

BASE = "https://nafnid.arnastofnun.is/django/vefur/api/"
DELAY_SECONDS = 1.0  # pause between paginated requests


def resolve_out_stem(stem: str) -> Path:
    """A bare stem (no path separator) writes into NAFNID_DATA_DIR; anything
    containing a separator (relative or absolute) is used as given, so old
    --out invocations with an explicit path still work unchanged."""
    p = Path(stem)
    return p if len(p.parts) > 1 else NAFNID_DATA_DIR / stem


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "research-script/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_all_pages(endpoint, params=None, max_pages=200):
    """Follow the `next` field until exhausted. Returns list of records."""
    params = params or {}
    url = f"{BASE}{endpoint}/"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    all_results = []
    page_num = 0
    while url and page_num < max_pages:
        page_num += 1
        print(f"  fetching page {page_num}: {url}")
        data = fetch_json(url)

        if isinstance(data, dict) and "results" in data:
            all_results.extend(data["results"])
            url = data.get("next")
        elif isinstance(data, list):
            all_results.extend(data)
            url = None
        else:
            # single object, not a list endpoint
            all_results.append(data)
            url = None

        if url:
            time.sleep(DELAY_SECONDS)

    return all_results


def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_csv(records, path):
    if not records:
        print(f"No records to write for {path}")
        return
    keys = []
    for r in records:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in keys:
                    keys.append(k)
    if not keys:
        print(f"Records aren't flat dicts, skipping CSV for {path} (JSON still saved)")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in records:
            writer.writerow(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("endpoint", help="endpoint name, e.g. ornefni, baeir, hreppar, geoleit")
    ap.add_argument("--out", required=True, help="output file stem; a bare name (no path "
                                                   f"separator) resolves under {NAFNID_DATA_DIR}, "
                                                   "otherwise used as given")
    ap.add_argument("--params", nargs="*", default=[],
                     help="extra query params as key=value pairs, e.g. --params type=farm limit=100")
    ap.add_argument("--max-pages", type=int, default=200)
    args = ap.parse_args()

    params = {}
    for p in args.params:
        if "=" not in p:
            ap.error(f"--params entries must be key=value, got: {p}")
        k, v = p.split("=", 1)
        params[k] = v

    print(f"Pulling endpoint={args.endpoint!r} params={params}")
    results = fetch_all_pages(args.endpoint, params=params, max_pages=args.max_pages)
    print(f"Total records: {len(results)}")

    out_stem = resolve_out_stem(args.out)
    save_json(results, f"{out_stem}.json")
    save_csv(results, f"{out_stem}.csv")
    print(f"Wrote {out_stem}.json and {out_stem}.csv")


if __name__ == "__main__":
    main()