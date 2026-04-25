"""
One-time seed: extract Places_Authority from the XLSX into place_names_authority.csv.

Run once, then edit the CSV by hand to clean up and extend variant names.
Safe to re-run — it will NOT overwrite an existing file unless you pass --overwrite.

Usage:
    python seed_place_names.py
    python seed_place_names.py --overwrite   # replace existing file
"""

import argparse
import csv
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import AUTHORITY_FILE

OUT_PATH = Path(__file__).parent / "place_names_authority.csv"

FIELDS = ["place_id", "canonical_name", "wikidata_id", "variants"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if OUT_PATH.exists() and not args.overwrite:
        print(f"{OUT_PATH.name} already exists. Pass --overwrite to replace it.")
        print("Edit it by hand to add/correct variants, then rerun the pipeline.")
        return

    df = pd.read_excel(AUTHORITY_FILE, sheet_name="Places_Authority", dtype=str).fillna("")

    rows = []
    for _, row in df.iterrows():
        pid          = row.get("place_id", "").strip()
        canonical    = row.get("canonical_name", "").strip()
        variants_raw = row.get("variant_names", "").strip()
        wikidata_id  = row.get("wikidata_id", "").strip() if "wikidata_id" in row else ""

        if not pid or not canonical:
            continue

        # Deduplicate variants; remove any that duplicate the canonical name
        parts = [v.strip() for v in variants_raw.split(";") if v.strip()]
        parts = [v for v in parts if v.lower() != canonical.lower()]
        variants = "; ".join(dict.fromkeys(parts))  # preserve order, deduplicate

        rows.append({
            "place_id":      pid,
            "canonical_name": canonical,
            "wikidata_id":   wikidata_id,
            "variants":      variants,
        })

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Seeded {len(rows)} places → {OUT_PATH}")
    print("Next: open place_names_authority.csv, correct/extend the 'variants' column,")
    print("then rerun 03_resolve_entities.py or 04b_propagate_corrections.py.")


if __name__ == "__main__":
    main()
