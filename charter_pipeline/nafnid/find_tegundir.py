#!/usr/bin/env python3
"""
Extract every distinct `tegund` (place-category) value seen inside the
nested `baeir` field of a textaleit CSV pull, without hitting the API
again. Useful for discovering the full place-type taxonomy (church,
klaustur, þingstaður, etc.) beyond just "Bær".

Usage:
    python find_tegundir.py kirkja_search.csv
"""

import ast
import csv
import sys
from collections import Counter

def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    path = sys.argv[1]
    tegund_counts = Counter()
    examples = {}

    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("baeir", "")
            if not raw or raw == "[]":
                continue
            try:
                baeir = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                continue
            for b in baeir:
                t = b.get("tegund", {})
                name = t.get("tegund") if isinstance(t, dict) else t
                if name:
                    tegund_counts[name] += 1
                    if name not in examples:
                        examples[name] = b.get("baejarnafn")

    print(f"{len(tegund_counts)} distinct place-tegund values found:\n")
    for name, count in tegund_counts.most_common():
        print(f"  {name!r:30s} count={count:6d}  e.g. {examples[name]!r}")


if __name__ == "__main__":
    main()