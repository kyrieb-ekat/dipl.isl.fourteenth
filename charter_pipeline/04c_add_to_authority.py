"""
Step 4c: Promote place rows tagged review_status=add into place_names_authority.csv.

Run this after 04b --annotate + manual review, once you have marked rows with
review_status=add (new places that should live in the authority for future runs).

Usage:
    python 04c_add_to_authority.py --csv output/review/vol04_places_new_geocoded.csv
    python 04c_add_to_authority.py --csv output/review/vol04_places_new_geocoded.csv --dry-run
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from place_authority import AUTHORITY_PATH, PlaceAuthority

# Review CSV column → authority CSV column
_COL_MAP = {
    "place_id":        "place_id",
    "canonical_name":  "canonical_name",
    "variant_names":   "variants",
    "coordinates_lat": "x(N) coords",
    "coordinates_long":"y(W) coords",
    "notes":           "notes",
}

_AUTHORITY_HEADERS = [
    "place_id", "canonical_name", "wikidata_id", "variants",
    "x(N) coords", "y(W) coords", "modern country", "notes",
]


def _wikidata_id(row: dict) -> str:
    return (row.get("wikidata_id") or row.get("proposed_wikidata_id") or "").strip()


def _modern_country(row: dict) -> str:
    return (row.get("modern_equivalent") or row.get("region") or "").strip()


def add_to_authority(csv_path: Path, dry_run: bool = False):
    auth = PlaceAuthority()
    existing_ids = {e.place_id for e in auth.entries}

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    to_add = [r for r in rows if (r.get("review_status") or "").strip().lower() == "add"]

    if not to_add:
        print("No rows tagged review_status=add. Nothing to do.")
        return

    new_entries = []
    skipped_existing = []
    for row in to_add:
        pid = (row.get("place_id") or "").strip()
        canonical = (row.get("canonical_name") or "").strip()
        if not pid or not canonical:
            print(f"  [warn] Skipping row with missing place_id or canonical_name: {row}")
            continue
        if pid in existing_ids:
            skipped_existing.append(pid)
            continue
        entry = {
            "place_id":      pid,
            "canonical_name": canonical,
            "wikidata_id":   _wikidata_id(row),
            "variants":      (row.get("variant_names") or "").strip(),
            "x(N) coords":   (row.get("coordinates_lat") or "").strip(),
            "y(W) coords":   (row.get("coordinates_long") or "").strip(),
            "modern country": _modern_country(row),
            "notes":         (row.get("notes") or "").strip(),
        }
        new_entries.append(entry)
        print(f"  + {pid} {canonical!r}  [{entry['wikidata_id'] or 'no QID'}]")

    if skipped_existing:
        print(f"  Skipped {len(skipped_existing)} already in authority: {', '.join(skipped_existing)}")

    if not new_entries:
        print("Nothing new to add after deduplication.")
        return

    if dry_run:
        print(f"\n[dry-run] Would add {len(new_entries)} entries. No file written.")
        return

    # Read raw authority file to preserve formatting/extra columns
    auth_rows = []
    auth_fieldnames = _AUTHORITY_HEADERS
    if AUTHORITY_PATH.exists():
        with open(AUTHORITY_PATH, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            auth_fieldnames = list(reader.fieldnames or _AUTHORITY_HEADERS)
            auth_rows = list(reader)

    bak = AUTHORITY_PATH.with_suffix(".csv.bak")
    shutil.copy2(AUTHORITY_PATH, bak)

    # Ensure all new entry keys are in fieldnames
    for key in _AUTHORITY_HEADERS:
        if key not in auth_fieldnames:
            auth_fieldnames.append(key)

    auth_rows.extend(new_entries)

    with open(AUTHORITY_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=auth_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(auth_rows)

    print(f"\nAdded {len(new_entries)} entries to {AUTHORITY_PATH.name}  (backup: {bak.name})")


def main():
    parser = argparse.ArgumentParser(description="Promote add-tagged places into place_names_authority.csv.")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"Error: {args.csv} not found.")
        sys.exit(1)

    add_to_authority(args.csv, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
