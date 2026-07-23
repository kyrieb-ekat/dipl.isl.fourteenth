"""
One-time migration: fold each vol{N}_places_new_geocoded.csv's data back into
the corresponding vol{N}_places_new.csv, so places_new.csv becomes the single
source of truth (matching how persons already work -- one file, no separate
geocoded copy). Run this once by hand per already-affected volume, then
04_lookup_coords.py writes coordinates into places_new.csv directly going
forward and the _geocoded.csv files can be deleted.

Not wired into any UI button or pipeline step -- this is a deliberate,
one-time cleanup for the divergence that already existed before places_new.csv
was made canonical, not a repeating step.

Usage:
    python migrate_geocoded_places.py            # migrates every vol with a
                                                   # _geocoded.csv on disk
    python migrate_geocoded_places.py --vol 4     # a single volume
    python migrate_geocoded_places.py --dry-run   # report only, no writes
"""
import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import REVIEW_DIR

# Fields to backfill only when the places_new.csv row currently has them
# blank -- the live file is the source of truth, the geocoded snapshot is a
# possibly-stale copy and must never clobber a value already present.
_FILL_IF_BLANK = ["coordinates_lat", "coordinates_long", "wikidata_id"]
_VOL04_STYLE_EXTRA = ["review_status", "proposed_place_id", "proposed_wikidata_id"]


def _score(row: dict) -> float:
    try:
        return float(row.get("geo_match_score", "") or -1)
    except ValueError:
        return -1


def migrate_one(geocoded_path: Path, dry_run: bool = False) -> dict:
    """
    Matches by canonical_name (case/whitespace-insensitive), NOT place_id.

    Confirmed against real vol01 data: place_id is not a stable join key
    between these two files -- ids for not-yet-authority-promoted places are
    allocated by encounter order in 03_resolve_entities.py, so re-running
    that step (e.g. after the segmentation redesign changed charter counts)
    reassigns them. Real example: places_new.csv's l106 is "Hamborg" today,
    but the geocoded snapshot's l106 is "Edínaborg" -- a place_id-scoped
    join would have silently attributed Edínaborg's coordinates to Hamborg.
    name-based matching, checked directly against the same real data, has
    zero names with conflicting non-blank coordinate sets, so it's the safe
    join key here.
    """
    places_path = geocoded_path.with_name(
        geocoded_path.name.replace("_new_geocoded.csv", "_new.csv")
    )
    if not places_path.exists():
        raise FileNotFoundError(f"No corresponding places file: {places_path}")

    places_df = pd.read_csv(places_path, dtype=str).fillna("")
    geo_df = pd.read_csv(geocoded_path, dtype=str).fillna("")
    geo_by_name: dict[str, list[dict]] = {}
    for r in geo_df.to_dict("records"):
        geo_by_name.setdefault(r["canonical_name"].strip().lower(), []).append(r)

    filled, no_match, ambiguous = 0, 0, 0
    matched_names: set[str] = set()

    for idx, row in places_df.iterrows():
        name = row["canonical_name"].strip().lower()
        candidates = geo_by_name.get(name, [])
        if not candidates:
            no_match += 1
            continue
        matched_names.add(name)

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            chosen = max(candidates, key=_score)
            ambiguous += 1
            print(f"  [ambiguous] {row['place_id']} {row['canonical_name']!r}: "
                  f"{len(candidates)} geocoded rows share this exact name "
                  f"(possibly under different place_ids); picked highest "
                  f"geo_match_score ({chosen.get('geo_match_score', '')})")

        changed = False
        for col in _FILL_IF_BLANK:
            if not row.get(col, "").strip() and chosen.get(col, "").strip():
                places_df.at[idx, col] = chosen[col]
                changed = True
        if chosen.get("geo_match_score", "").strip():
            places_df.at[idx, "geo_match_score"] = chosen["geo_match_score"]
            changed = True
        for col in _VOL04_STYLE_EXTRA:
            if col in chosen and not row.get(col, "").strip() and chosen.get(col, "").strip():
                places_df.at[idx, col] = chosen[col]
                changed = True
        if changed:
            filled += 1

    unmatched_geocoded_names = sorted(set(geo_by_name.keys()) - matched_names)
    if unmatched_geocoded_names:
        print(f"  [orphaned] {len(unmatched_geocoded_names)} geocoded name(s) with no "
              f"counterpart in {places_path.name} (not migrated, likely merged/renamed "
              f"since the geocoded snapshot): {unmatched_geocoded_names}")

    if dry_run:
        print(f"  [dry-run] Would update {places_path.name}: {filled} rows filled, "
              f"{no_match} with no name match at all, {ambiguous} ambiguous "
              f"(same name, multiple geocoded rows).")
    else:
        bak = places_path.with_suffix(".csv.bak")
        shutil.copy2(places_path, bak)
        places_df.to_csv(places_path, index=False)
        print(f"  Updated {places_path.name} ({filled} rows filled) -- backup: {bak.name}")

    return {
        "filled": filled, "no_match": no_match, "ambiguous": ambiguous,
        "unmatched_geocoded_names": unmatched_geocoded_names,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fold vol{N}_places_new_geocoded.csv back into vol{N}_places_new.csv."
    )
    parser.add_argument("--vol", type=int, help="Migrate a single volume only.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.vol:
        targets = [REVIEW_DIR / f"vol{args.vol:02d}_places_new_geocoded.csv"]
    else:
        targets = sorted(REVIEW_DIR.glob("*_places_new_geocoded.csv"))

    if not targets:
        print("No *_places_new_geocoded.csv files found. Nothing to migrate.")
        return

    for path in targets:
        if not path.exists():
            print(f"Error: {path} not found.")
            continue
        print(f"\n=== {path.name} ===")
        migrate_one(path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
