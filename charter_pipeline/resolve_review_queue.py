"""
Applies accept/reject decisions recorded in vol{N}_review_queue.csv, turning
an ambiguous fuzzy-match flag into real data instead of a cosmetic column.

- accept: the REVIEW:{match_id} placeholder token (embedded throughout
  resolved_entities.json and charters.csv by 03_resolve_entities.py) is
  replaced with the plain match_id -- the proposed existing-authority match
  is adopted.
- reject: a genuinely new person/place id is minted instead, and a full New
  Entities row is added to persons_new.csv/places_new.csv so it flows into
  the same review_status (ok/skip/add) + Authority Browser + Person
  Duplicates + nafnid reconciliation workflow every other new entity goes
  through. The REVIEW: token is relinked to this new id.
- blank decision: left untouched.

There's no id column linking a review_queue.csv row to its exact entry in
resolved_entities.json, so the join is positional: rows are grouped by
(charter_filename, type) in file order, matched 1:1 against the
REVIEW:-tagged entries of that type in that charter's resolved_persons/
resolved_locations list, also in list order. Verified against real vol04
data (244 rows, zero mismatches) including a case where content-based
matching alone (name + match_id + score) would have been ambiguous.

Resolved rows are archived to vol{N}_review_queue_resolved.csv (with the
outcome id and a timestamp) and removed from the live queue.
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import ENTITIES_DIR, REVIEW_DIR

# Mirrors 03_resolve_entities.py's next_id()/charter_year() -- not imported
# since that module's filename starts with a digit (same precedent as
# 04d_add_to_person_authority.py copying 04c's _resolve_fieldnames).
_CHARTER_YEAR_RE = re.compile(r"(\d{3,4})")


def charter_year(date_str) -> str:
    m = _CHARTER_YEAR_RE.match((date_str or "").strip())
    if not m:
        return ""
    return str(int(m.group(1)))


def next_id(existing_ids: list[str], prefix: str) -> str:
    nums = [int(i[1:]) for i in existing_ids if i.startswith(prefix) and i[1:].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


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

# Same grouping 05b_rescan_flags.py uses for _has_review_* -- kept here only
# for the accept/reject relink step; flag recomputation itself is delegated
# to 05b via subprocess, not duplicated.
_PERSON_ID_COLS = ["grantor_id", "recipient_id"]
_PLACE_ID_COLS = ["location_written_id", "location_hearing_id"]


def resolve_review_queue(vol: int) -> dict:
    """
    Applies decision=accept/reject from vol{N}_review_queue.csv into
    resolved_entities.json, charters.csv, and persons_new.csv/places_new.csv.
    Archives processed rows and removes them from the live queue. Returns a
    summary dict; never raises for a positional join failure -- that row is
    reported and left untouched instead.
    """
    prefix = f"vol{vol:02d}"
    queue_path = REVIEW_DIR / f"{prefix}_review_queue.csv"
    resolved_path = ENTITIES_DIR / f"{prefix}_resolved_entities.json"
    charters_path = REVIEW_DIR / f"{prefix}_charters.csv"
    persons_path = REVIEW_DIR / f"{prefix}_persons_new.csv"
    places_path = REVIEW_DIR / f"{prefix}_places_new.csv"
    archive_path = REVIEW_DIR / f"{prefix}_review_queue_resolved.csv"

    for required in (queue_path, resolved_path, charters_path):
        if not required.exists():
            raise FileNotFoundError(f"Required file not found: {required}")

    qdf = pd.read_csv(queue_path, dtype=str).fillna("")
    with open(resolved_path, encoding="utf-8") as f:
        charters = json.load(f)
    cdf = pd.read_csv(charters_path, dtype=str).fillna("")
    pdf = (pd.read_csv(persons_path, dtype=str).fillna("") if persons_path.exists()
           else pd.DataFrame(columns=PERSON_FIELDS))
    plf = (pd.read_csv(places_path, dtype=str).fillna("") if places_path.exists()
           else pd.DataFrame(columns=PLACE_FIELDS))

    # Positional index: (charter_filename, type) -> ordered list of
    # {"charter": ch, "item": entry_dict} for every REVIEW:-tagged entry.
    by_group: dict[tuple, list[dict]] = {}
    for ch in charters:
        fn = ch.get("filename")
        for p in ch.get("resolved_persons", []):
            if str(p.get("person_id", "")).startswith("REVIEW:"):
                by_group.setdefault((fn, "person"), []).append({"charter": ch, "item": p})
        for loc in ch.get("resolved_locations", []):
            if str(loc.get("place_id", "")).startswith("REVIEW:"):
                by_group.setdefault((fn, "place"), []).append({"charter": ch, "item": loc})

    group_cursor: dict[tuple, int] = {}
    existing_person_ids = pdf["person_id"].tolist() if "person_id" in pdf.columns else []
    existing_place_ids = plf["place_id"].tolist() if "place_id" in plf.columns else []
    new_person_rows: list[dict] = []
    new_place_rows: list[dict] = []

    accepted_idx: list[int] = []
    rejected_idx: list[int] = []
    rejected_new_ids: list[str] = []
    join_failures: list[dict] = []
    outcome_by_idx: dict[int, str] = {}

    for idx, row in qdf.iterrows():
        fn = row["charter_filename"]
        typ = row["type"]
        key = (fn, typ)
        occ_i = group_cursor.get(key, 0)
        group_cursor[key] = occ_i + 1

        decision = row["decision"].strip().lower()
        if decision not in ("accept", "reject"):
            continue

        occurrences = by_group.get(key, [])
        if occ_i >= len(occurrences):
            join_failures.append({
                "row_index": int(idx), "charter_filename": fn, "type": typ,
                "reason": "no corresponding REVIEW: entry at this position",
            })
            continue

        entry = occurrences[occ_i]
        ch = entry["charter"]
        item = entry["item"]
        id_field = "person_id" if typ == "person" else "place_id"
        match_id = row["match_id"]
        old_token = f"REVIEW:{match_id}"

        # Locate the one charters.csv row for this charter via `sequence`,
        # which 05_export_csvs.py copies verbatim from the same resolved
        # charter dict and is unique within a volume.
        seq = ch.get("sequence")
        crow_mask = cdf["sequence"].astype(str) == str(seq)

        if decision == "accept":
            new_id = match_id
        else:
            if typ == "person":
                new_id = next_id(existing_person_ids + [r["person_id"] for r in new_person_rows], "p")
            else:
                new_id = next_id(existing_place_ids + [r["place_id"] for r in new_place_rows], "l")

            source_ref = f"DI vol.{vol} seq.{ch.get('sequence', '?')} | {ch.get('di_reference', '')}"
            if typ == "person":
                new_person_rows.append({
                    "person_id": new_id,
                    "canonical_name": row["extracted_name"],
                    "variant_names": "",
                    "patronymic": "",
                    "occupation": item.get("role_category", "") or row.get("role_category", ""),
                    "title": item.get("qualifier", "") or "",
                    "floruit_start": charter_year(ch.get("date")),
                    "floruit_end": charter_year(ch.get("date")),
                    "gender": "",
                    "associated_places": "",
                    "notes": "",
                    "sources": source_ref,
                })
            else:
                new_place_rows.append({
                    "place_id": new_id,
                    "canonical_name": row["extracted_name"],
                    "variant_names": "",
                    "place_type": "",
                    "coordinates_lat": "",
                    "coordinates_long": "",
                    "region": item.get("region", ""),
                    "district": "",
                    "modern_equivalent": "",
                    "notes": "",
                    "sources": source_ref,
                })
            rejected_new_ids.append(new_id)

        # Relink resolved_entities.json (mutate the actual dict in place).
        item[id_field] = new_id

        # Relink charters.csv for this one charter row.
        if typ == "person":
            for col in _PERSON_ID_COLS:
                if col in cdf.columns:
                    cdf.loc[crow_mask & cdf[col].eq(old_token), col] = new_id
            if "persons_by_role" in cdf.columns:
                cdf.loc[crow_mask, "persons_by_role"] = cdf.loc[crow_mask, "persons_by_role"].apply(
                    lambda s: s.replace(f"[{old_token}]", f"[{new_id}]")
                )
        else:
            for col in _PLACE_ID_COLS:
                if col in cdf.columns:
                    cdf.loc[crow_mask & cdf[col].eq(old_token), col] = new_id
            if "locations_mentioned_ids" in cdf.columns:
                def _relink_list(s: str) -> str:
                    parts = [new_id if p.strip() == old_token else p.strip()
                             for p in s.split(";") if p.strip()]
                    return "; ".join(dict.fromkeys(parts))
                cdf.loc[crow_mask, "locations_mentioned_ids"] = (
                    cdf.loc[crow_mask, "locations_mentioned_ids"].apply(_relink_list)
                )

        outcome_by_idx[idx] = new_id
        if decision == "accept":
            accepted_idx.append(idx)
        else:
            rejected_idx.append(idx)

    processed_idx = accepted_idx + rejected_idx

    # Archive processed rows before dropping them from the live queue.
    rows_archived = 0
    if processed_idx:
        archive_rows = []
        resolved_at = datetime.now().isoformat(timespec="seconds")
        for idx in processed_idx:
            r = qdf.loc[idx].to_dict()
            r["outcome_id"] = outcome_by_idx[idx]
            r["resolved_at"] = resolved_at
            archive_rows.append(r)
        new_archive = pd.DataFrame(archive_rows)
        if archive_path.exists():
            existing_archive = pd.read_csv(archive_path, dtype=str).fillna("")
            combined = pd.concat([existing_archive, new_archive], ignore_index=True)
        else:
            combined = new_archive
        combined.to_csv(archive_path, index=False)
        rows_archived = len(archive_rows)

    qdf_remaining = qdf.drop(index=processed_idx)
    qdf_remaining.to_csv(queue_path, index=False)

    with open(resolved_path, "w", encoding="utf-8") as f:
        json.dump(charters, f, ensure_ascii=False, indent=2)
    cdf.to_csv(charters_path, index=False)

    if new_person_rows:
        pdf = pd.concat([pdf, pd.DataFrame(new_person_rows)], ignore_index=True)
    pdf.to_csv(persons_path, index=False)

    if new_place_rows:
        plf = pd.concat([plf, pd.DataFrame(new_place_rows)], ignore_index=True)
    plf.to_csv(places_path, index=False)

    # Recompute _has_review_* flags via the existing, already-tested script.
    # Pass --csv (this module's own resolved charters_path) rather than --vol,
    # so it always operates on the exact file just written here -- --vol would
    # resolve its own path from config.REVIEW_DIR in a separate process and
    # silently diverge whenever REVIEW_DIR is overridden for testing.
    subprocess.run(
        [sys.executable, str(Path(__file__).parent / "05b_rescan_flags.py"), "--csv", str(charters_path)],
        check=True, cwd=str(Path(__file__).parent),
    )

    return {
        "accepted": len(accepted_idx),
        "rejected": len(rejected_idx),
        "rejected_new_ids": rejected_new_ids,
        "skipped_blank": int((qdf["decision"].str.strip() == "").sum()),
        "join_failures": join_failures,
        "rows_removed": len(processed_idx),
        "rows_archived": rows_archived,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Apply review-queue accept/reject decisions into the New Entities workflow."
    )
    parser.add_argument("--vol", type=int, required=True)
    args = parser.parse_args()

    summary = resolve_review_queue(args.vol)
    print(f"Accepted: {summary['accepted']}  Rejected: {summary['rejected']}"
          f" (new ids: {summary['rejected_new_ids']})")
    print(f"Skipped (blank decision): {summary['skipped_blank']}")
    print(f"Rows removed from queue: {summary['rows_removed']}  Archived: {summary['rows_archived']}")
    if summary["join_failures"]:
        print(f"WARNING: {len(summary['join_failures'])} row(s) could not be matched positionally:")
        for jf in summary["join_failures"]:
            print(f"  row {jf['row_index']}: {jf['charter_filename']} ({jf['type']}) - {jf['reason']}")


if __name__ == "__main__":
    main()
