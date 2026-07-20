#!/usr/bin/env python3
"""
Pull place-name records from nafnid.is's internal geoleit API and save
as JSON + CSV.

Endpoint discovered via browser devtools (Network tab):
    https://nafnid.arnastofnun.is/django/vefur/api/geoleit/?type=<type>&limit=<n>

Usage:
    python pull_nafnid.py --type farm --out farm
    python pull_nafnid.py --type natural --out natural
    python pull_nafnid.py --list-types      # just prints guesses to try

NOTE: This hits nafnid.arnastofnun.is directly (not the nafnid.is front
end, which disallows automated access via robots.txt). Treat any pulled
data as a personal working copy for reconciliation/research use only
until terms are confirmed with Árnastofnun's onomastics department.
"""

import argparse
import csv
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import NAFNID_DATA_DIR

BASE_URL = "https://nafnid.arnastofnun.is/django/vefur/api/geoleit/"

# Known field order from a sample "farm" record - CSV writer will fall
# back to the union of all keys seen if a record has extra fields.
FIELDNAMES_HINT = [
    "id", "type", "name", "baer_id", "baer_name", "ornefnaskra",
    "hreppur", "hreppur_id", "sysla", "sysla_id", "lat", "lng",
    "tegund", "tegund_id", "article_count",
]

# Guesses worth trying for the `type` param - swap in whatever you find
# in the map UI's own layer/legend controls.
CANDIDATE_TYPES = ["farm", "natural", "place", "ornefni", "parish", "kirkja"]


def resolve_out_stem(stem: str) -> Path:
    """A bare stem (no path separator) writes into NAFNID_DATA_DIR; anything
    containing a separator (relative or absolute) is used as given, so old
    --out invocations with an explicit path still work unchanged."""
    p = Path(stem)
    return p if len(p.parts) > 1 else NAFNID_DATA_DIR / stem


def fetch_type(place_type: str, limit: int = 20000, timeout: int = 30):
    url = f"{BASE_URL}?type={place_type}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "research-script/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data


def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_csv(records, path):
    if not records:
        print(f"No records to write for {path}")
        return
    # union of all keys across records, keeping the hinted order first
    keys = list(FIELDNAMES_HINT)
    for r in records:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in records:
            writer.writerow(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", help="value for the `type` query param, e.g. farm")
    ap.add_argument("--out", help="output file stem (writes <out>.json and <out>.csv); "
                                    "a bare name (no path separator) resolves under "
                                    f"{NAFNID_DATA_DIR}, otherwise used as given")
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--list-types", action="store_true",
                     help="try each candidate type and report counts, no full download")
    args = ap.parse_args()

    if args.list_types:
        for t in CANDIDATE_TYPES:
            try:
                data = fetch_type(t, limit=1)
                count = data.get("count", "?")
                print(f"{t!r:12s} -> count={count}")
            except urllib.error.HTTPError as e:
                print(f"{t!r:12s} -> HTTP {e.code}")
            except Exception as e:
                print(f"{t!r:12s} -> error: {e}")
            time.sleep(0.5)
        return

    if not args.type or not args.out:
        ap.error("--type and --out are required unless using --list-types")

    print(f"Fetching type={args.type!r} ...")
    data = fetch_type(args.type, limit=args.limit)
    count = data.get("count")
    results = data.get("results", [])
    print(f"API reports count={count}, got {len(results)} records")

    out_stem = resolve_out_stem(args.out)
    save_json(data, f"{out_stem}.json")
    save_csv(results, f"{out_stem}.csv")
    print(f"Wrote {out_stem}.json and {out_stem}.csv")


if __name__ == "__main__":
    main()