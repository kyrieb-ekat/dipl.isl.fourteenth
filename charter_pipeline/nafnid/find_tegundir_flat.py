#!/usr/bin/env python3
"""
Count distinct `tegund` (place/entity-category) values in a flat
baeir.csv pull, where each row's `tegund` column is a single dict
like "{'id': 1, 'tegund': 'Bær'}" rather than a nested list.

Usage:
    python find_tegundir_flat.py baeir.csv
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
    counts = Counter()
    examples = {}

    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("tegund", "")
            if not raw:
                continue
            try:
                t = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                continue
            name = t.get("tegund") if isinstance(t, dict) else t
            if name:
                counts[name] += 1
                if name not in examples:
                    examples[name] = row.get("baejarnafn")

    print(f"{len(counts)} distinct tegund values found:\n")
    for name, count in counts.most_common():
        print(f"  {name!r:30s} count={count:6d}  e.g. {examples[name]!r}")


if __name__ == "__main__":
    main()