"""
Step 5b: Rescan vol{N}_charters.csv and rewrite _has_review_persons / _has_review_places flags.

Run this after manually resolving REVIEW: prefixes in charters.csv so that
06_merge_into_xlsx.py can pick up the previously blocked charters.

Also run after 04b to pick up place_id corrections that may have cleared REVIEW: prefixes.

Usage:
    python 05b_rescan_flags.py --vol 4
    python 05b_rescan_flags.py --csv output/review/vol04_charters.csv
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import REVIEW_DIR

# Which columns carry person IDs vs place IDs
_PERSON_ID_COLS = {"grantor_id", "recipient_id", "persons_by_role"}
_PLACE_ID_COLS  = {"location_written_id", "location_hearing_id", "locations_mentioned_ids"}


def _has_review(row: dict, cols: set[str]) -> bool:
    return any("REVIEW:" in (row.get(c) or "") for c in cols)


def rescan(csv_path: Path):
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if "_has_review_persons" not in fieldnames:
        fieldnames.append("_has_review_persons")
    if "_has_review_places" not in fieldnames:
        fieldnames.append("_has_review_places")
    if "_has_parse_error" not in fieldnames:
        fieldnames.append("_has_parse_error")

    cleared_persons = 0
    cleared_places = 0
    still_flagged_persons = 0
    still_flagged_places = 0

    for row in rows:
        old_p = row.get("_has_review_persons", "")
        old_pl = row.get("_has_review_places", "")

        new_p  = "Y" if _has_review(row, _PERSON_ID_COLS) else ""
        new_pl = "Y" if _has_review(row, _PLACE_ID_COLS)  else ""

        row["_has_review_persons"] = new_p
        row["_has_review_places"]  = new_pl

        if old_p == "Y" and new_p == "":
            cleared_persons += 1
        elif new_p == "Y":
            still_flagged_persons += 1

        if old_pl == "Y" and new_pl == "":
            cleared_places += 1
        elif new_pl == "Y":
            still_flagged_places += 1

    bak = csv_path.with_suffix(".csv.bak")
    shutil.copy2(csv_path, bak)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Rescanned {len(rows)} charters in {csv_path.name}.")
    if cleared_persons or cleared_places:
        print(f"  Cleared: {cleared_persons} person flags, {cleared_places} place flags.")
    if still_flagged_persons or still_flagged_places:
        print(f"  Still flagged: {still_flagged_persons} person, {still_flagged_places} place "
              f"(resolve remaining REVIEW: prefixes, then re-run).")
    if not cleared_persons and not cleared_places and not still_flagged_persons and not still_flagged_places:
        print("  No review flags found — all charters are clean.")
    print(f"  (backup: {bak.name})")


def main():
    parser = argparse.ArgumentParser(
        description="Rescan charters.csv and rewrite _has_review_* flags based on current REVIEW: prefixes."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--vol", type=int, help="Volume number.")
    group.add_argument("--csv", type=Path, help="Direct path to a charters CSV.")
    args = parser.parse_args()

    if args.csv:
        csv_path = args.csv
    else:
        csv_path = REVIEW_DIR / f"vol{args.vol:02d}_charters.csv"

    if not csv_path.exists():
        print(f"Error: {csv_path} not found.")
        sys.exit(1)

    rescan(csv_path)


if __name__ == "__main__":
    main()
