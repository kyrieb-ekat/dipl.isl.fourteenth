#!/usr/bin/env python3
"""
Convert a nafnid.is geoleit API response (or a truncated copy/paste of
one) into a clean CSV.

Handles two cases:
  1. Well-formed JSON: {"count":N,"next":null,"previous":null,"results":[...]}
  2. Truncated/malformed text (e.g. copy-pasted from devtools and cut
     off mid-object) - falls back to regex-extracting every complete
     {...} object it can find and skips the dangling tail.

Usage:
    python json_to_csv.py raw_response.txt output.csv
"""

import csv
import json
import re
import sys

FIELDNAMES_HINT = [
    "id", "type", "name", "baer_id", "baer_name", "ornefnaskra",
    "hreppur", "hreppur_id", "sysla", "sysla_id", "lat", "lng",
    "tegund", "tegund_id", "article_count",
]


def try_clean_json_parse(text: str):
    """Try standard parse; also try trivial repairs for a truncated tail."""
    try:
        data = json.loads(text)
        return data.get("results", data if isinstance(data, list) else [])
    except json.JSONDecodeError:
        pass

    # Try trimming back to the last complete "},{" boundary inside
    # a results array, then closing it off.
    idx = text.rfind("},{")
    if idx != -1:
        repaired = text[: idx + 1] + "]}"
        # find the start of the results array to prefix correctly
        start = text.find('"results":[')
        if start != -1:
            repaired = text[: start + len('"results":[')] + text[start + len('"results":['): idx + 1] + "]}"
        try:
            data = json.loads(repaired)
            return data.get("results", [])
        except json.JSONDecodeError:
            pass
    return None


def extract_objects_by_regex(text: str):
    """Fallback: find every balanced {...} object at the top level of
    the results list via a simple brace-counting scan. Skips any
    trailing object that never closes (the truncated one)."""
    objects = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict) and "id" in obj and "name" in obj:
                        objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    return objects


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]
    with open(in_path, "r", encoding="utf-8") as f:
        text = f.read()

    results = try_clean_json_parse(text)
    if not results:
        print("Clean parse failed or was empty - falling back to regex extraction...")
        results = extract_objects_by_regex(text)

    print(f"Extracted {len(results)} complete records")
    if not results:
        print("No records found - check the input file.")
        sys.exit(1)

    keys = list(FIELDNAMES_HINT)
    for r in results:
        for k in r.keys():
            if k not in keys:
                keys.append(k)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()