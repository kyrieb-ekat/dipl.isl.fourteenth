"""
Step 5: Load resolved entity data into the DB's charters/charter_persons/
charter_places tables.

Usage:
    python 05_export_csvs.py --vol 1
    python 05_export_csvs.py --vol 1 --force   # replace an already-loaded volume

Reads:  output/entities/vol{N}_resolved_entities.json

Writes: charter_pipeline.db -- one charters row per charter, one
        charter_persons/charter_places row per resolved_persons/
        resolved_locations entry. Persons/places themselves are NOT minted
        here -- 03_resolve_entities.py already inserted them directly into
        the DB at resolution time (see that script's module docstring);
        this step wires up the junction rows and, for pending-review
        entries, both the review_match_person_pk/review_match_place_pk
        pointer AND a review_queue_items row (so the Review Queue tab shows
        a freshly-processed volume's ambiguous matches immediately, not
        just already-migrated volumes whose review_queue_items came from
        migrate_to_sqlite.py's one-time positional-join pass).

Not idempotent by design (matches the old CSV pipeline's behavior of
overwriting output files whole): re-running for a volume that already has
charters in the DB is an error unless --force is passed, since
charters.UNIQUE(volume, sequence) would otherwise raise on every row.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import ENTITIES_DIR
import db


def _str_or_blank(v) -> str:
    return "" if v is None else str(v)


def _parse_ref(raw_id) -> tuple[int | None, int | None]:
    """raw_id is an int person_pk/place_pk (as written by 03_resolve_entities.py),
    a "REVIEW:{pk}" string, or None/''. Returns (pk, review_candidate_pk) --
    exactly one of the two is non-None (or both None for a blank id)."""
    if isinstance(raw_id, str) and raw_id.startswith("REVIEW:"):
        return None, int(raw_id.split(":", 1)[1])
    if raw_id in (None, ""):
        return None, None
    return int(raw_id), None


def _delete_volume_charters(volume: int) -> int:
    """Removes every charters row (and, via ON DELETE CASCADE, every
    charter_persons/charter_places row) for `volume`. Does NOT touch
    persons/places themselves -- they may be referenced elsewhere (e.g.
    already promoted to canonical, or referenced by another volume after a
    merge). Also clears any review_queue_items rows pointing at this
    volume's charters first: schema.sql gives charter_persons/charter_places
    ON DELETE CASCADE from charters, but NOT review_queue_items -- with
    PRAGMA foreign_keys=ON (set by db.get_connection()), deleting charters
    that still have queue rows pointing at them would otherwise raise a
    foreign key constraint error (a real scenario for a volume that already
    has queue rows from the original CSV-to-SQLite migration)."""
    conn = db.get_connection()
    try:
        with conn:
            n = conn.execute("SELECT COUNT(*) FROM charters WHERE volume=?", (volume,)).fetchone()[0]
            conn.execute(
                "DELETE FROM review_queue_items WHERE charter_pk IN "
                "(SELECT charter_pk FROM charters WHERE volume=?)", (volume,),
            )
            conn.execute("DELETE FROM charters WHERE volume=?", (volume,))
        return n
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Load resolved entities into charters/charter_persons/charter_places.")
    parser.add_argument("--vol", type=int, required=True)
    parser.add_argument("--force", action="store_true",
                         help="Delete this volume's existing charters (and junction rows) before re-inserting.")
    args = parser.parse_args()

    resolved_path = ENTITIES_DIR / f"vol{args.vol:02d}_resolved_entities.json"
    if not resolved_path.exists():
        print(f"Error: {resolved_path} not found. Run 03_resolve_entities.py first.", file=sys.stderr)
        sys.exit(1)

    with open(resolved_path, encoding="utf-8") as f:
        charters = json.load(f)

    existing = db.get_charters(volume=args.vol)
    if len(existing) > 0:
        if not args.force:
            print(
                f"Error: vol{args.vol:02d} already has {len(existing)} charter(s) in the database -- "
                f"re-running 05_export_csvs.py would create duplicates; this script is not idempotent "
                f"by design, matching the old CSV pipeline's behavior of overwriting output files whole. "
                f"Pass --force to delete the existing rows and re-insert.",
                file=sys.stderr,
            )
            sys.exit(1)
        removed = _delete_volume_charters(args.vol)
        print(f"--force: removed {removed} existing charter(s) (and their junction rows) for vol{args.vol:02d}.")

    n_charters = n_persons = n_places = n_review_persons = n_review_places = 0

    for ch in charters:
        has_error = "_parse_error" in ch or "_api_error" in ch
        sequence = ch.get("sequence")

        charter_pk = db.create_charter(
            args.vol, sequence,
            date=ch.get("date") or "",
            doc_type=ch.get("doc_type") or "",
            subject=ch.get("subject") or "",
            outcome=ch.get("outcome") or "",
            scribe=ch.get("scribe") or "",
            scribe_source=ch.get("scribe_source") or "",
            seal_info=ch.get("seal_info") or "",
            language=ch.get("language") or "",
            notes=ch.get("_parse_error") or ch.get("_api_error") or "",
            shelfmark_auto=f"DI Bindi {args.vol}, seq. {ch.get('sequence', '?')} (p.{ch.get('page_start', '?')})",
            di_reference=ch.get("di_reference") or "",
            date_uncertain=_str_or_blank(ch.get("date_uncertain")),
            date_header=ch.get("date_header") or "",
            has_parse_error=has_error,
        )
        n_charters += 1

        for ordinal, p in enumerate(ch.get("resolved_persons", [])):
            pk, review_pk = _parse_ref(p.get("person_id"))
            resolution_state = "pending_review" if review_pk is not None else "resolved"
            if resolution_state == "pending_review":
                n_review_persons += 1
            charter_person_pk = db.add_charter_person(
                charter_pk, ordinal,
                p.get("role_category") or "", p.get("name") or "",
                person_pk=pk, resolution_state=resolution_state,
                review_match_person_pk=review_pk, match_score=p.get("match_score"),
                qualifier=p.get("qualifier") or "",
            )
            n_persons += 1
            if resolution_state == "pending_review":
                match_row = db.get_person_by_pk(review_pk)
                db.create_review_item(
                    "person", charter_pk, p.get("name") or "", review_pk, p.get("match_score"),
                    charter_person_pk=charter_person_pk, role_category=p.get("role_category") or "",
                    closest_match=match_row["canonical_name"] if match_row else "",
                    charter_date=ch.get("date") or "",
                )

        for ordinal, loc in enumerate(ch.get("resolved_locations", [])):
            pk, review_pk = _parse_ref(loc.get("place_id"))
            resolution_state = "pending_review" if review_pk is not None else "resolved"
            if resolution_state == "pending_review":
                n_review_places += 1
            charter_place_pk = db.add_charter_place(
                charter_pk, ordinal,
                loc.get("role") or "", loc.get("name") or "",
                place_pk=pk, resolution_state=resolution_state,
                review_match_place_pk=review_pk, match_score=loc.get("match_score"),
                region=loc.get("region") or "",
            )
            n_places += 1
            if resolution_state == "pending_review":
                match_row = db.get_place_by_pk(review_pk)
                db.create_review_item(
                    "place", charter_pk, loc.get("name") or "", review_pk, loc.get("match_score"),
                    charter_place_pk=charter_place_pk, role=loc.get("role") or "",
                    closest_match=match_row["canonical_name"] if match_row else "",
                    charter_date=ch.get("date") or "",
                )

    flags = db.rescan_review_flags(args.vol)

    print(f"Loaded {n_charters} charter(s) for vol{args.vol:02d} into the database.")
    print(f"  {n_persons} charter_persons row(s) ({n_review_persons} pending review)")
    print(f"  {n_places} charter_places row(s) ({n_review_places} pending review)")
    print(f"  rescan_review_flags: {flags}")
    print("\nNext steps:")
    print("  1. Resolve any pending_review rows (Review Queue tab / db.apply_review_decision).")
    print("  2. Run 04_lookup_coords.py to geocode new places.")
    print("  3. When satisfied, promote eligible new entities to canonical status.")


if __name__ == "__main__":
    main()
