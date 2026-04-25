"""
Step 4b: Reconcile place names against place_names_authority.csv.

For each row in the places CSV, look up the canonical_name (and any listed
variant_names) in the authority file and assign the correct wikidata_id.
Nothing else is changed — every row stays as its own record.

This is the OpenRefine reconciliation step: name string → Wikidata QID.

Usage:
    python 04b_propagate_corrections.py --csv output/review/vol01_places_new.csv
    python 04b_propagate_corrections.py --csv output/review/vol01_places_new.csv --dry-run
    python 04b_propagate_corrections.py --csv output/review/vol01_places_new_geocoded.csv --annotate

--annotate mode:
    Writes proposed_place_id, proposed_wikidata_id, and review_status columns into
    the CSV without applying any changes. Unmatched rows get review_status=no_match.
    Open the CSV in a spreadsheet and fill in review_status per row:
        ok       — apply the proposed change
        skip     — leave the row unchanged
        add      — apply AND flag this row for 04c_add_to_authority.py
        (blank)  — apply (default, same as ok)
    Then re-run without --annotate to apply selectively.
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from place_authority import PlaceAuthority, split_variants

_WARN_VARIANTS_THRESHOLD = 20  # log a notice for unexpectedly long variant lists

_EXTRA_COLS = ["review_status", "proposed_place_id", "proposed_wikidata_id"]


def parse_variants(raw: str) -> list[str]:
    parts = split_variants(raw)
    if len(parts) > _WARN_VARIANTS_THRESHOLD:
        print(f"  [warn] variant_names field has {len(parts)} entries — check for data quality issues")
    return parts


def _apply_status(review_status: str) -> bool:
    """Return True if this row's proposed changes should be applied."""
    return review_status.strip().lower() in ("", "ok", "add")


def reconcile(csv_path: Path, dry_run: bool = False, annotate: bool = False):
    auth = PlaceAuthority()
    if not auth.entries:
        print("Authority file is empty or missing — nothing to reconcile.")
        return

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    # Ensure extra columns exist in fieldnames
    for col in _EXTRA_COLS:
        if col not in fieldnames:
            fieldnames.append(col)

    has_status_col = "review_status" in (reader.fieldnames or [])

    updated = 0
    unmatched = []

    for row in rows:
        # Ensure extra columns have a value
        for col in _EXTRA_COLS:
            row.setdefault(col, "")

        canonical = (row.get("canonical_name") or "").strip()
        current_qid = (row.get("wikidata_id") or "").strip()
        current_pid = (row.get("place_id") or "").strip()
        variants = parse_variants(row.get("variant_names") or "")
        review_status = (row.get("review_status") or "").strip().lower()

        entry = auth.find(canonical, current_qid, variants)

        if not entry:
            unmatched.append(f"  {current_pid} {canonical!r}")
            if annotate and not row["review_status"]:
                row["review_status"] = "no_match"
            continue

        new_qid = entry.wikidata_id
        new_pid = entry.place_id

        if annotate:
            # Write proposed values without applying; don't overwrite existing status
            row["proposed_place_id"] = new_pid if new_pid and new_pid != current_pid else ""
            row["proposed_wikidata_id"] = new_qid if new_qid and new_qid != current_qid else ""
            continue

        # Apply mode — respect review_status when the column exists
        if has_status_col and not _apply_status(review_status):
            continue

        changed = False
        if new_qid and new_qid != current_qid:
            print(f"  {current_pid} {canonical!r}: wikidata_id "
                  f"{current_qid or '(none)'} → {new_qid}  [{entry.canonical_name}]")
            if not dry_run:
                row["wikidata_id"] = new_qid
            changed = True
        if new_pid and new_pid != current_pid:
            print(f"  {current_pid} {canonical!r}: place_id "
                  f"{current_pid or '(none)'} → {new_pid}  [{entry.canonical_name}]")
            if not dry_run:
                row["place_id"] = new_pid
            changed = True
        if changed:
            updated += 1

    if annotate:
        bak = csv_path.with_suffix(".csv.bak")
        shutil.copy2(csv_path, bak)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        matched = len(rows) - len(unmatched)
        print(f"Annotated {matched} matched rows, {len(unmatched)} unmatched (review_status=no_match).")
        print(f"Open {csv_path.name}, fill in review_status, then re-run without --annotate.")
        print(f"(backup: {bak.name})")
        return

    print(f"\n{updated} wikidata_ids updated.")
    if unmatched:
        print(f"{len(unmatched)} rows not in authority file (add them to place_names_authority.csv to reconcile):")
        for line in unmatched:
            print(line)

    if dry_run:
        print("\n[dry-run] No changes written.")
        return

    bak = csv_path.with_suffix(".csv.bak")
    shutil.copy2(csv_path, bak)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved: {csv_path}  (backup: {bak.name})")


def main():
    parser = argparse.ArgumentParser(description="Reconcile place wikidata_ids against authority file.")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    parser.add_argument("--annotate", action="store_true",
                        help="Write proposed columns + review_status into CSV without applying.")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"Error: {args.csv} not found.")
        sys.exit(1)

    if args.dry_run and args.annotate:
        print("Error: --dry-run and --annotate are mutually exclusive.")
        sys.exit(1)

    reconcile(args.csv, dry_run=args.dry_run, annotate=args.annotate)


if __name__ == "__main__":
    main()
