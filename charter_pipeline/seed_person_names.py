"""
One-time seed: extract persons_authority from the XLSX into person_names_authority.csv.

Run once, then edit the CSV by hand to clean up and extend variant names.
Safe to re-run — will NOT overwrite an existing file unless you pass --overwrite.

Usage:
    python seed_person_names.py
    python seed_person_names.py --overwrite   # replace existing file
"""

import argparse
import csv
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import AUTHORITY_FILE

OUT_PATH = Path(__file__).parent / "person_names_authority.csv"

FIELDS = [
    "person_id", "canonical_name", "wikidata_id", "variants",
    "patronymic", "occupation", "title",
    "floruit_start", "floruit_end", "gender", "notes",
]

# XLSX column → CSV column (only columns that need remapping)
_COL_REMAP = {
    "variant_names": "variants",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if OUT_PATH.exists() and not args.overwrite:
        print(f"{OUT_PATH.name} already exists. Pass --overwrite to replace it.")
        print("Edit it by hand to add/correct variants, then rerun the pipeline.")
        return

    try:
        df = pd.read_excel(AUTHORITY_FILE, sheet_name="persons_authority", dtype=str).fillna("")
    except Exception as e:
        print(f"Error reading XLSX: {e}", file=sys.stderr)
        sys.exit(1)

    # Normalize column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    rows = []
    for _, row in df.iterrows():
        pid       = row.get("person_id", "").strip()
        canonical = row.get("canonical_name", "").strip()
        if not pid or not canonical:
            continue

        variants_raw = row.get("variants", row.get("variant_names", "")).strip()
        parts = [v.strip() for v in variants_raw.split(";") if v.strip()]
        parts = [v for v in parts if v.lower() != canonical.lower()]
        variants = "; ".join(dict.fromkeys(parts))

        rows.append({
            "person_id":     pid,
            "canonical_name": canonical,
            "wikidata_id":   row.get("wikidata_id", "").strip(),
            "variants":      variants,
            "patronymic":    row.get("patronymic", "").strip(),
            "occupation":    row.get("occupation", "").strip(),
            "title":         row.get("title", "").strip(),
            "floruit_start": row.get("floruit_start", "").strip(),
            "floruit_end":   row.get("floruit_end", "").strip(),
            "gender":        row.get("gender", "").strip(),
            "notes":         row.get("notes", "").strip(),
        })

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Seeded {len(rows)} persons → {OUT_PATH}")
    print("Next: open person_names_authority.csv, correct/extend the 'variants' column,")
    print("then rerun 03_resolve_entities.py.")


if __name__ == "__main__":
    main()
