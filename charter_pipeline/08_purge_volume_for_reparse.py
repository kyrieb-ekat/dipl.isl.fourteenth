"""
Purge a volume's stale provisional persons/places/charters before re-running
the extraction pipeline against corrected segmentation.

Why this exists: 05_export_csvs.py's --force only deletes a volume's
charters (and, via ON DELETE CASCADE, its charter_persons/charter_places
junction rows) -- it never touches the persons/places rows themselves,
since those are minted earlier by 03_resolve_entities.py, not by 05. Simply
re-running 02->03->05 for a volume whose segmentation was fixed after the
first pass would leave the OLD provisional persons/places sitting in the DB
as orphans (no longer referenced by any charter) alongside a fresh,
overlapping set minted by the rerun.

Usage:
    python 08_purge_volume_for_reparse.py --vol 1              # dry run (default)
    python 08_purge_volume_for_reparse.py --vol 1 --confirm    # actually delete

Deletion order matters for FK safety (db.get_connection() sets
PRAGMA foreign_keys=ON, and persons/places FKs from charter_persons/
charter_places/*_duplicate_candidates have no ON DELETE CASCADE):

1. person_duplicate_candidates / place_duplicate_candidates rows that
   reference this volume's provisional persons/places -- these have no
   cascade from persons/places, so they must go first.
2. review_queue_items -> charters -> (cascades) charter_persons/
   charter_places for this volume, via 05_export_csvs.py's own
   _delete_volume_charters (imported directly rather than reimplemented,
   since it already gets the review_queue_items-before-charters ordering
   right). This clears every remaining reference to this volume's
   provisional persons/places from charter_persons.person_pk/place_pk.
3. Only now can the provisional persons/places themselves be deleted
   without hitting a dangling FK reference.

Only ever deletes status='provisional' rows -- a canonical/authority row
must never be removed by this script, even if it happens to share this
volume's source_volume (seen in practice: legacy authority rows carry
source_volume=NULL, never a real volume, but the filter is kept explicit
and defensive rather than relying on that).
"""
import argparse
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db

_export_csvs_spec = importlib.util.spec_from_file_location(
    "_export_csvs_05", Path(__file__).parent / "05_export_csvs.py"
)
_export_csvs = importlib.util.module_from_spec(_export_csvs_spec)
_export_csvs_spec.loader.exec_module(_export_csvs)


def _count_provisional(conn, table: str, pk_col: str, volume: int) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE source_volume=? AND status='provisional'",
        (volume,),
    ).fetchone()[0]


def _count_person_dup_candidates(conn, volume: int) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM person_duplicate_candidates pdc
           WHERE EXISTS (SELECT 1 FROM persons p WHERE p.person_pk IN (pdc.person_a_pk, pdc.person_b_pk)
                         AND p.source_volume=? AND p.status='provisional')""",
        (volume,),
    ).fetchone()[0]


def _count_place_dup_candidates(conn, volume: int) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM place_duplicate_candidates pdc
           WHERE EXISTS (SELECT 1 FROM places pl WHERE pl.place_pk = pdc.place_pk
                         AND pl.source_volume=? AND pl.status='provisional')""",
        (volume,),
    ).fetchone()[0]


def summarize(volume: int) -> dict:
    conn = db.get_connection()
    try:
        return {
            "charters": conn.execute(
                "SELECT COUNT(*) FROM charters WHERE volume=?", (volume,)
            ).fetchone()[0],
            "persons": _count_provisional(conn, "persons", "person_pk", volume),
            "places": _count_provisional(conn, "places", "place_pk", volume),
            "person_duplicate_candidates": _count_person_dup_candidates(conn, volume),
            "place_duplicate_candidates": _count_place_dup_candidates(conn, volume),
        }
    finally:
        conn.close()


def purge(volume: int) -> dict:
    conn = db.get_connection()
    try:
        with conn:
            n_pdc = conn.execute(
                """DELETE FROM person_duplicate_candidates
                   WHERE EXISTS (SELECT 1 FROM persons p WHERE p.person_pk IN (person_a_pk, person_b_pk)
                                 AND p.source_volume=? AND p.status='provisional')""",
                (volume,),
            ).rowcount
            n_plc = conn.execute(
                """DELETE FROM place_duplicate_candidates AS pdc
                   WHERE EXISTS (SELECT 1 FROM places pl WHERE pl.place_pk = pdc.place_pk
                                 AND pl.source_volume=? AND pl.status='provisional')""",
                (volume,),
            ).rowcount
    finally:
        conn.close()

    n_charters = _export_csvs._delete_volume_charters(volume)

    conn = db.get_connection()
    try:
        with conn:
            n_persons = conn.execute(
                "DELETE FROM persons WHERE source_volume=? AND status='provisional'", (volume,)
            ).rowcount
            n_places = conn.execute(
                "DELETE FROM places WHERE source_volume=? AND status='provisional'", (volume,)
            ).rowcount
    finally:
        conn.close()

    return {
        "charters": n_charters,
        "persons": n_persons,
        "places": n_places,
        "person_duplicate_candidates": n_pdc,
        "place_duplicate_candidates": n_plc,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Purge a volume's stale provisional persons/places/charters before reparsing."
    )
    parser.add_argument("--vol", type=int, required=True)
    parser.add_argument("--confirm", action="store_true",
                         help="Actually delete. Without this flag, only prints what would be deleted.")
    args = parser.parse_args()

    counts = summarize(args.vol)
    label = "Would delete" if not args.confirm else "About to delete"
    print(f"vol{args.vol:02d} -- {label}:")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    if not args.confirm:
        print("\nDry run only -- pass --confirm to actually delete.")
        return

    result = purge(args.vol)
    print(f"\nDeleted for vol{args.vol:02d}:")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
