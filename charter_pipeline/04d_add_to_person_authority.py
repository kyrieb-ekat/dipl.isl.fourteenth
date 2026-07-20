"""
Step 4d: Promote person rows tagged review_status=add into person_names_authority.csv.

Run this after manually reviewing persons_new.csv and marking rows with
review_status=add (persons that should be in the authority for future reconciliation).

Usage:
    python 04d_add_to_person_authority.py --csv output/review/vol04_persons_new.csv
    python 04d_add_to_person_authority.py --csv output/review/vol04_persons_new.csv --dry-run
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from person_authority import AUTHORITY_PATH, PersonAuthority

_AUTHORITY_HEADERS = [
    "person_id", "canonical_name", "wikidata_id", "variants",
    "patronymic", "occupation", "title",
    "floruit_start", "floruit_end", "gender", "notes",
]


def _normalize_header(name: str) -> str:
    """Whitespace/case-insensitive key for matching authority CSV column names."""
    return name.strip().lower()


def _resolve_fieldnames(real_fieldnames: list, canonical_headers: list) -> tuple:
    """
    Match `canonical_headers` against `real_fieldnames` (whatever is actually
    on disk) using a normalized comparison, so a stray whitespace difference
    never causes a duplicate column to be appended. Mirrors the equivalent
    helper in 04c_add_to_authority.py -- this file's real header currently
    matches _AUTHORITY_HEADERS exactly, so this is a preemptive fix for the
    same bug shape, not yet triggered here.

    Returns (fieldnames, norm_to_real) -- see 04c_add_to_authority.py for details.
    """
    fieldnames = list(real_fieldnames)
    norm_to_real = {_normalize_header(f): f for f in fieldnames}
    for canonical in canonical_headers:
        if _normalize_header(canonical) not in norm_to_real:
            fieldnames.append(canonical)
            norm_to_real[_normalize_header(canonical)] = canonical
    return fieldnames, norm_to_real


def _map_row(row: dict) -> dict:
    return {
        "person_id":      (row.get("person_id") or "").strip(),
        "canonical_name": (row.get("canonical_name") or "").strip(),
        "wikidata_id":    (row.get("wikidata_id") or "").strip(),
        "variants":       (row.get("variant_names") or "").strip(),
        "patronymic":     (row.get("patronymic") or "").strip(),
        "occupation":     (row.get("occupation") or "").strip(),
        "title":          (row.get("title") or "").strip(),
        "floruit_start":  (row.get("floruit_start") or "").strip(),
        "floruit_end":    (row.get("floruit_end") or "").strip(),
        "gender":         (row.get("gender") or "").strip(),
        "notes":          (row.get("notes") or "").strip(),
    }


def add_to_authority(csv_path: Path, dry_run: bool = False):
    auth = PersonAuthority()
    existing_ids = {e.person_id for e in auth.entries}

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    to_add = [r for r in rows if (r.get("review_status") or "").strip().lower() == "add"]

    if not to_add:
        print("No rows tagged review_status=add. Nothing to do.")
        return

    new_entries = []
    skipped_existing = []
    for row in to_add:
        entry = _map_row(row)
        pid = entry["person_id"]
        canonical = entry["canonical_name"]
        if not pid or not canonical:
            print(f"  [warn] Skipping row with missing person_id or canonical_name: {row}")
            continue
        if pid in existing_ids:
            skipped_existing.append(pid)
            continue
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

    auth_rows = []
    real_fieldnames = _AUTHORITY_HEADERS
    if AUTHORITY_PATH.exists():
        with open(AUTHORITY_PATH, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            real_fieldnames = list(reader.fieldnames or _AUTHORITY_HEADERS)
            auth_rows = list(reader)

    bak = AUTHORITY_PATH.with_suffix(".csv.bak")
    shutil.copy2(AUTHORITY_PATH, bak)

    auth_fieldnames, norm_to_real = _resolve_fieldnames(real_fieldnames, _AUTHORITY_HEADERS)

    remapped_entries = [
        {norm_to_real[_normalize_header(k)]: v for k, v in entry.items()}
        for entry in new_entries
    ]
    auth_rows.extend(remapped_entries)

    with open(AUTHORITY_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=auth_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(auth_rows)

    print(f"\nAdded {len(new_entries)} entries to {AUTHORITY_PATH.name}  (backup: {bak.name})")


def main():
    parser = argparse.ArgumentParser(description="Promote add-tagged persons into person_names_authority.csv.")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"Error: {args.csv} not found.")
        sys.exit(1)

    add_to_authority(args.csv, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
