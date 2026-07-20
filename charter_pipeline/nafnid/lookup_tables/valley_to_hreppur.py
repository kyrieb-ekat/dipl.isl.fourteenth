"""
Match a valley/fjord/district name (as mentioned in DI, e.g.
"Hjaltadalr", "Eyjafjörður", "Skagaströnd") against nafnid's own
hreppur names, which very often embed the valley/fjord name directly
(Svarfaðardalshreppur, Fnjóskadalshreppur, Hörgárdalshreppur, etc.).

This avoids hand-building a valley->hreppur lookup table from
scratch - instead it mines baeir.csv's own hreppur column for
substring matches against a normalized version of the DI mention.

Usage:
    python valley_to_hreppur.py baeir.csv "Hjaltadal"
    python valley_to_hreppur.py baeir.csv "Eyjafjörður"

Or import candidates_for_valley() directly in reconcile.py to add
valley-based blocking alongside sysla-based blocking.
"""

import ast
import csv
import re
import sys
import unicodedata
from collections import defaultdict


def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = strip_accents(s)
    # strip common Icelandic suffixes that won't appear in the DI
    # mention but do appear in the hreppur's compound name
    s = re.sub(r"(ur|s)?$", "", s)
    return s


def extract_hreppur_name(raw: str) -> str:
    m = re.search(r"'nafn':\s*'([^']*)'", raw or "")
    return m.group(1) if m else ""


def build_hreppur_index(baeir_csv_path: str):
    """Returns dict: normalized hreppur name -> set of raw hreppur names
    (there can be more than one hreppur containing a given valley
    root, e.g. multiple 'Svínadalshreppur' historically in different
    sýslur - that's fine, sysla-blocking upstream will disambiguate)."""
    seen = defaultdict(set)
    with open(baeir_csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hreppur = extract_hreppur_name(row.get("hreppur", ""))
            if hreppur:
                seen[normalize(hreppur)].add(hreppur)
    return seen


def candidates_for_valley(valley_name: str, hreppur_index: dict):
    """Substring match: does the normalized valley name appear inside
    a normalized hreppur name, or vice versa? Loose by design - meant
    to generate a candidate SET for further sysla/name cross-checking,
    not a final answer on its own."""
    target = normalize(valley_name)
    if not target:
        return []
    matches = []
    for norm_hreppur, raws in hreppur_index.items():
        if target in norm_hreppur or norm_hreppur in target:
            matches.extend(raws)
    return sorted(set(matches))


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    baeir_csv, valley_name = sys.argv[1], sys.argv[2]
    index = build_hreppur_index(baeir_csv)
    matches = candidates_for_valley(valley_name, index)
    print(f"Candidate hreppar for {valley_name!r}:")
    for m in matches:
        print(f"  {m}")
    if not matches:
        print("  (no substring matches - valley name may not be embedded "
              "in any hreppur name, fall back to sysla-level blocking)")


if __name__ == "__main__":
    main()