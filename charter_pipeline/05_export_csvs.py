"""
Step 5: Export per-volume review CSVs from resolved entity data.

Usage:
    python 05_export_csvs.py --vol 1

Reads:  output/entities/vol{N}_resolved_entities.json

Writes (all in output/review/):
    vol{N}_charters.csv       — one row per charter, for review before merging into Charter_Data
    vol{N}_persons_new.csv    — candidate new person rows (to add to persons_authority)
    vol{N}_places_new.csv     — candidate new place rows (to add to Places_Authority)
    vol{N}_review_queue.csv   — ambiguous matches requiring manual decision

These CSVs are meant for you to review, edit, and approve.
Only approved rows are merged into the XLSX by 06_merge_into_xlsx.py.
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import ENTITIES_DIR, REVIEW_DIR


def flatten_persons(resolved_persons: list[dict]) -> dict[str, str]:
    """
    Build columns matching your raw extraction format:
        persons_by_role: "priest-issuer: Jón Koðrason; layman-issuer: Geirr Þorsteinsson; ..."
        grantor_id / recipient_id: first matching person_id (for Charter_Data FK columns)
    """
    by_role: dict[str, list[str]] = defaultdict(list)
    grantor_id = ""
    recipient_id = ""

    for p in resolved_persons:
        role = p.get("role_category", "unknown")
        name = p.get("name", "")
        pid  = p.get("person_id", "")
        display = f"{name} [{pid}]" if pid else name
        by_role[role].append(display)

        if not grantor_id and "issuer" in role:
            grantor_id = pid
        if not recipient_id and role == "recipient":
            recipient_id = pid

    persons_str = "; ".join(f"{role}: {', '.join(names)}" for role, names in by_role.items())
    return {
        "persons_by_role": persons_str,
        "grantor_id": grantor_id,
        "recipient_id": recipient_id,
    }


def flatten_locations(resolved_locations: list[dict]) -> dict[str, str]:
    loc_writing = ""
    loc_hearing = ""
    loc_writing_id = ""
    loc_hearing_id = ""
    all_ids = []

    for loc in resolved_locations:
        role = loc.get("role", "")
        name = loc.get("name", "")
        pid  = loc.get("place_id", "")
        region = loc.get("region", "")
        display = f"{name} ({region})" if region else name
        if pid:
            all_ids.append(pid)

        if role == "loc.writing" and not loc_writing:
            loc_writing = display
            loc_writing_id = pid
        elif role == "loc.hearing" and not loc_hearing:
            loc_hearing = display
            loc_hearing_id = pid

    return {
        "location_written": loc_writing,
        "location_written_id": loc_writing_id,
        "location_hearing": loc_hearing,
        "location_hearing_id": loc_hearing_id,
        "locations_mentioned_ids": "; ".join(dict.fromkeys(all_ids)),  # deduplicated, ordered
    }


CHARTER_FIELDS = [
    # Identifiers / source
    "charter_id_placeholder", "volume", "sequence", "page_start",
    "shelfmark_auto",         # auto-constructed; verify against physical shelfmark
    "di_reference",
    # Core data
    "date", "date_uncertain", "date_header",
    "doc_type", "subject", "outcome",
    # Persons
    "scribe", "scribe_source",
    "grantor_id", "recipient_id", "persons_by_role",
    # Locations
    "location_written", "location_written_id",
    "location_hearing", "location_hearing_id",
    "locations_mentioned_ids",
    # Other
    "seal_info", "language", "notes",
    # Flags for your review
    "_has_parse_error", "_has_review_persons", "_has_review_places",
]

PERSON_FIELDS = [
    "person_id", "canonical_name", "variant_names", "patronymic",
    "occupation", "title", "floruit_start", "floruit_end",
    "gender", "associated_places", "notes", "sources",
]

PLACE_FIELDS = [
    "place_id", "canonical_name", "variant_names", "place_type",
    "coordinates_lat", "coordinates_long", "region", "district",
    "modern_equivalent", "notes", "sources",
]


def main():
    parser = argparse.ArgumentParser(description="Export per-volume review CSVs.")
    parser.add_argument("--vol", type=int, required=True)
    args = parser.parse_args()

    resolved_path = ENTITIES_DIR / f"vol{args.vol:02d}_resolved_entities.json"
    if not resolved_path.exists():
        print(f"Error: {resolved_path} not found. Run 03_resolve_entities.py first.", file=sys.stderr)
        sys.exit(1)

    with open(resolved_path, encoding="utf-8") as f:
        charters = json.load(f)

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    charter_rows = []
    new_persons_seen: dict[str, dict] = {}
    new_places_seen:  dict[str, dict] = {}

    for ch in charters:
        has_error = "_parse_error" in ch or "_api_error" in ch

        person_flat = flatten_persons(ch.get("resolved_persons", []))
        loc_flat    = flatten_locations(ch.get("resolved_locations", []))

        has_review_p = any("REVIEW:" in p.get("person_id", "") for p in ch.get("resolved_persons", []))
        has_review_l = any("REVIEW:" in l.get("place_id", "")  for l in ch.get("resolved_locations", []))

        row = {
            "charter_id_placeholder": f"c_vol{args.vol:02d}_seq{ch.get('sequence', '?'):04}" if isinstance(ch.get('sequence'), int) else f"c_vol{args.vol:02d}_seq{ch.get('sequence', '?')}",
            "volume":       ch.get("volume", ""),
            "sequence":     ch.get("sequence", ""),
            "page_start":   ch.get("page_start", ""),
            "shelfmark_auto": f"DI Bindi {args.vol}, seq. {ch.get('sequence', '?')} (p.{ch.get('page_start', '?')})",
            "di_reference": ch.get("di_reference", ""),
            "date":         ch.get("date", ""),
            "date_uncertain": ch.get("date_uncertain", ""),
            "date_header":  ch.get("date_header", ""),
            "doc_type":     ch.get("doc_type", ""),
            "subject":      ch.get("subject", ""),
            "outcome":      ch.get("outcome", ""),
            "scribe":       ch.get("scribe", ""),
            "scribe_source": ch.get("scribe_source", ""),
            "seal_info":    ch.get("seal_info", ""),
            "language":     ch.get("language", ""),
            "notes":        ch.get("_parse_error", "") or ch.get("_api_error", ""),
            "_has_parse_error":    "Y" if has_error else "",
            "_has_review_persons": "Y" if has_review_p else "",
            "_has_review_places":  "Y" if has_review_l else "",
            **person_flat,
            **loc_flat,
        }
        charter_rows.append(row)

        # Collect new persons and places (deduplicated across charters)
        for p in ch.get("resolved_persons", []):
            pid = p.get("person_id", "")
            if pid and not pid.startswith("REVIEW:") and not pid.startswith("p"):
                # This is a genuinely new person (temp ID starting with p is still new if not in authority)
                pass
        # Pull new persons/places from the raw resolved data
        for pid in ch.get("new_persons", []):
            if pid not in new_persons_seen:
                # Find the person data in resolved_persons
                for p in ch.get("resolved_persons", []):
                    if p.get("person_id") == pid:
                        new_persons_seen[pid] = {
                            "person_id": pid,
                            "canonical_name": p.get("name", ""),
                            "variant_names": "",
                            "patronymic": "",
                            "occupation": p.get("role_category", ""),
                            "title": p.get("qualifier", "") or "",
                            "floruit_start": "",
                            "floruit_end": "",
                            "gender": "",
                            "associated_places": "",
                            "notes": "",
                            "sources": ch.get("di_reference", ""),
                        }
                        break

        for lid in ch.get("new_places", []):
            if lid not in new_places_seen:
                for loc in ch.get("resolved_locations", []):
                    if loc.get("place_id") == lid:
                        new_places_seen[lid] = {
                            "place_id": lid,
                            "canonical_name": loc.get("name", ""),
                            "variant_names": "",
                            "place_type": "",
                            "coordinates_lat": "",
                            "coordinates_long": "",
                            "region": loc.get("region", ""),
                            "district": "",
                            "modern_equivalent": "",
                            "notes": "",
                            "sources": ch.get("di_reference", ""),
                        }
                        break

    def write_csv(path: Path, rows: list[dict], fields: list[str]):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  → {path.name}  ({len(rows)} rows)")

    prefix = f"vol{args.vol:02d}"
    write_csv(REVIEW_DIR / f"{prefix}_charters.csv",     charter_rows,                    CHARTER_FIELDS)
    write_csv(REVIEW_DIR / f"{prefix}_persons_new.csv",  list(new_persons_seen.values()), PERSON_FIELDS)
    write_csv(REVIEW_DIR / f"{prefix}_places_new.csv",   list(new_places_seen.values()),  PLACE_FIELDS)

    print(f"\nDone. Review CSVs in {REVIEW_DIR}")
    print("Next steps:")
    print("  1. Open and review each CSV.")
    print("  2. Run 04_lookup_coords.py to geocode new places.")
    print("  3. When satisfied, run 06_merge_into_xlsx.py to merge approved rows into the XLSX.")


if __name__ == "__main__":
    main()
