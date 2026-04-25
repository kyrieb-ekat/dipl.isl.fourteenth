"""
Step 6: Merge approved review CSVs into the authority XLSX.

Run this ONLY after you have reviewed and edited the CSVs from 05_export_csvs.py.

Usage:
    python 06_merge_into_xlsx.py --vol 1
    python 06_merge_into_xlsx.py --vol 1 --dry-run   # preview counts without writing

Reads:
    output/review/vol{N}_charters.csv
    output/review/vol{N}_persons_new.csv
    output/review/vol{N}_places_new.csv

Writes:
    CHARTER_authority_file_updated.xlsx  (sibling of the original; original is never modified)

Also exports:
    output/review/vol{N}_nodegoat_export.csv  — flattened export for nodegoat import
"""

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
from config import AUTHORITY_FILE, REVIEW_DIR

# Column mappings: review CSV field → Charter_Data sheet column
CHARTER_COL_MAP = {
    "charter_id_placeholder": "charter_id",
    "shelfmark_auto":         "shelfmark",
    "date":                   "date",
    "grantor_id":             "grantor_id",
    "recipient_id":           "recipient_id",
    "location_written_id":    "location_written_id",
    "locations_mentioned_ids": "locations_mentioned_ids",
    "subject":                "topic",
    "scribe":                 "scribe_clues",
    "seal_info":              "seal_info",
    "notes":                  "notes",
}


def load_sheet(wb, sheet_name: str) -> pd.DataFrame:
    ws = wb[sheet_name]
    data = list(ws.values)
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data[1:], columns=data[0])


def append_rows_to_sheet(wb, sheet_name: str, new_rows: list[dict], id_col: str):
    """Append rows that don't already exist (checked by id_col)."""
    ws = wb[sheet_name]
    existing_data = list(ws.values)
    if not existing_data:
        return 0

    headers = list(existing_data[0])
    existing_ids = {row[headers.index(id_col)] for row in existing_data[1:] if row[headers.index(id_col)]}

    appended = 0
    for row_dict in new_rows:
        row_id = row_dict.get(id_col, "")
        if row_id in existing_ids:
            continue
        row_values = [row_dict.get(h, "") for h in headers]
        ws.append(row_values)
        existing_ids.add(row_id)
        appended += 1

    return appended


def build_charter_row(csv_row: dict) -> dict:
    """Map review CSV columns to Charter_Data sheet columns."""
    out = {}
    for csv_col, sheet_col in CHARTER_COL_MAP.items():
        out[sheet_col] = csv_row.get(csv_col, "")
    # Combine location_written_id + location_hearing_id → location_written_id
    # (Charter_Data uses a single loc.writing field)
    if not out.get("location_written_id"):
        out["location_written_id"] = csv_row.get("location_hearing_id", "")
    return out


def build_nodegoat_row(csv_row: dict) -> dict:
    """Produce a flat row for nodegoat import (names instead of IDs)."""
    return {
        "charter_id":        csv_row.get("charter_id_placeholder", ""),
        "shelfmark":         csv_row.get("shelfmark_auto", ""),
        "di_reference":      csv_row.get("di_reference", ""),
        "date":              csv_row.get("date", ""),
        "date_uncertain":    csv_row.get("date_uncertain", ""),
        "doc_type":          csv_row.get("doc_type", ""),
        "subject":           csv_row.get("subject", ""),
        "outcome":           csv_row.get("outcome", ""),
        "persons_by_role":   csv_row.get("persons_by_role", ""),
        "scribe":            csv_row.get("scribe", ""),
        "scribe_source":     csv_row.get("scribe_source", ""),
        "location_written":  csv_row.get("location_written", ""),
        "location_hearing":  csv_row.get("location_hearing", ""),
        "locations_mentioned_ids": csv_row.get("locations_mentioned_ids", ""),
        "seal_info":         csv_row.get("seal_info", ""),
        "language":          csv_row.get("language", ""),
        "grantor_id":        csv_row.get("grantor_id", ""),
        "recipient_id":      csv_row.get("recipient_id", ""),
    }


def main():
    parser = argparse.ArgumentParser(description="Merge approved CSVs into the authority XLSX.")
    parser.add_argument("--vol", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Preview row counts without writing.")
    args = parser.parse_args()

    prefix = f"vol{args.vol:02d}"
    charters_csv = REVIEW_DIR / f"{prefix}_charters.csv"
    persons_csv  = REVIEW_DIR / f"{prefix}_persons_new.csv"
    places_csv   = REVIEW_DIR / f"{prefix}_places_new.csv"

    for path in [charters_csv, persons_csv, places_csv]:
        if not path.exists():
            print(f"Error: {path} not found. Run 05_export_csvs.py first.", file=sys.stderr)
            sys.exit(1)

    charters_df = pd.read_csv(charters_csv, dtype=str).fillna("")
    persons_df  = pd.read_csv(persons_csv,  dtype=str).fillna("")
    places_df   = pd.read_csv(places_csv,   dtype=str).fillna("")

    # Remove rows flagged for review (person/place IDs containing "REVIEW:")
    safe_charters = charters_df[charters_df["_has_review_persons"].ne("Y") &
                                charters_df["_has_review_places"].ne("Y") &
                                charters_df["_has_parse_error"].ne("Y")]
    flagged = len(charters_df) - len(safe_charters)
    print(f"Charters: {len(safe_charters)} ready to merge, {flagged} flagged (review/error — skipped).")

    # Gate persons on review_status if column is present
    _PERSON_APPLY = {"", "ok", "add"}
    if "review_status" in persons_df.columns:
        safe_persons = persons_df[persons_df["review_status"].str.strip().str.lower().isin(_PERSON_APPLY)]
        skipped_persons = len(persons_df) - len(safe_persons)
        print(f"New persons: {len(safe_persons)} to merge, {skipped_persons} skipped (review_status=skip).")
    else:
        safe_persons = persons_df
        print(f"New persons: {len(persons_df)} (no review_status column — merging all).")

    print(f"New places:  {len(places_df)}")

    if args.dry_run:
        print("\n[dry-run] No files written.")
        return

    # Make a copy of the authority file to work on
    out_path = AUTHORITY_FILE.parent / (AUTHORITY_FILE.stem + "_updated.xlsx")
    shutil.copy2(AUTHORITY_FILE, out_path)
    print(f"\nWorking on copy: {out_path.name}")

    wb = load_workbook(out_path)

    # Append persons (filtered by review_status if column present)
    person_rows = safe_persons.to_dict("records")
    n_persons = append_rows_to_sheet(wb, "persons_authority", person_rows, "person_id")
    print(f"  Appended {n_persons} new person rows to persons_authority.")

    # Append places
    place_rows = places_df.to_dict("records")
    n_places = append_rows_to_sheet(wb, "Places_Authority", place_rows, "place_id")
    print(f"  Appended {n_places} new place rows to Places_Authority.")

    # Append charters
    charter_rows = [build_charter_row(r) for r in safe_charters.to_dict("records")]
    n_charters = append_rows_to_sheet(wb, "Charter_Data", charter_rows, "charter_id")
    print(f"  Appended {n_charters} new charter rows to Charter_Data.")

    wb.save(out_path)
    print(f"\nSaved: {out_path}")

    # Export nodegoat CSV
    import csv
    nodegoat_rows = [build_nodegoat_row(r) for r in safe_charters.to_dict("records")]
    nodegoat_path = REVIEW_DIR / f"{prefix}_nodegoat_export.csv"
    if nodegoat_rows:
        with open(nodegoat_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(nodegoat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(nodegoat_rows)
        print(f"nodegoat export: {nodegoat_path}")


if __name__ == "__main__":
    main()
