"""
Cross-volume "Final Review" promotion: everything marked review_status=add
in any volume's persons_new.csv/places_new.csv, checked against the
duplicate-detection tools, and promoted into person_names_authority.csv /
place_names_authority.csv in one pass.

Duplicates 04c_add_to_authority.py's/04d_add_to_person_authority.py's core
promotion logic rather than subprocessing them -- both are digit-prefixed
(can't be `import`ed, same constraint as every other numbered pipeline
step), and more importantly this module needs to exclude specific
duplicate-blocked ids even when their row is otherwise review_status=add,
which the existing CLI scripts have no flag for.
"""
import csv
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import REVIEW_DIR
from person_authority import AUTHORITY_PATH as PERSON_AUTHORITY_PATH, PersonAuthority
from place_authority import AUTHORITY_PATH as PLACE_AUTHORITY_PATH, PlaceAuthority

PERSON_AUTHORITY_HEADERS = [
    "person_id", "canonical_name", "wikidata_id", "variants",
    "patronymic", "occupation", "title",
    "floruit_start", "floruit_end", "gender", "notes",
]

PLACE_AUTHORITY_HEADERS = [
    "place_id", "canonical_name", "wikidata_id", "variants",
    "x(N) coords", "y(W) coords", "modern country", "notes",
]


def _normalize_header(name: str) -> str:
    return name.strip().lower()


def _resolve_fieldnames(real_fieldnames: list, canonical_headers: list) -> tuple:
    """Mirrors 04c_add_to_authority.py's/04d_add_to_person_authority.py's
    helper of the same name -- matches canonical headers against whatever's
    really on disk, whitespace/case-insensitively, so a stray formatting
    difference never appends a duplicate column."""
    fieldnames = list(real_fieldnames)
    norm_to_real = {_normalize_header(f): f for f in fieldnames}
    for canonical in canonical_headers:
        if _normalize_header(canonical) not in norm_to_real:
            fieldnames.append(canonical)
            norm_to_real[_normalize_header(canonical)] = canonical
    return fieldnames, norm_to_real


def _map_person_row(row: dict) -> dict:
    return {
        "person_id": (row.get("person_id") or "").strip(),
        "canonical_name": (row.get("canonical_name") or "").strip(),
        "wikidata_id": (row.get("wikidata_id") or "").strip(),
        "variants": (row.get("variant_names") or "").strip(),
        "patronymic": (row.get("patronymic") or "").strip(),
        "occupation": (row.get("occupation") or "").strip(),
        "title": (row.get("title") or "").strip(),
        "floruit_start": (row.get("floruit_start") or "").strip(),
        "floruit_end": (row.get("floruit_end") or "").strip(),
        "gender": (row.get("gender") or "").strip(),
        "notes": (row.get("notes") or "").strip(),
    }


def _map_place_row(row: dict) -> dict:
    return {
        "place_id": (row.get("place_id") or "").strip(),
        "canonical_name": (row.get("canonical_name") or "").strip(),
        "wikidata_id": (row.get("wikidata_id") or row.get("proposed_wikidata_id") or "").strip(),
        "variants": (row.get("variant_names") or "").strip(),
        "x(N) coords": (row.get("coordinates_lat") or "").strip(),
        "y(W) coords": (row.get("coordinates_long") or "").strip(),
        "modern country": (row.get("modern_equivalent") or row.get("region") or "").strip(),
        "notes": (row.get("notes") or "").strip(),
    }


def _promote_rows(
    to_add: list[dict], exclude_ids: set[str], existing_ids: set[str], id_col: str,
    row_mapper, authority_path: Path, canonical_headers: list[str], dry_run: bool = False,
) -> dict:
    added, skipped_existing, skipped_blocked = [], [], []
    new_entries = []

    for row in to_add:
        rid = (row.get(id_col) or "").strip()
        canonical = (row.get("canonical_name") or "").strip()
        if not rid or not canonical:
            continue
        if rid in exclude_ids:
            skipped_blocked.append(rid)
            continue
        if rid in existing_ids:
            skipped_existing.append(rid)
            continue
        new_entries.append(row_mapper(row))
        added.append(rid)

    if dry_run or not new_entries:
        return {"added": added, "skipped_existing": skipped_existing, "skipped_blocked": skipped_blocked}

    auth_rows = []
    real_fieldnames = canonical_headers
    if authority_path.exists():
        with open(authority_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            real_fieldnames = list(reader.fieldnames or canonical_headers)
            auth_rows = list(reader)

    bak = authority_path.with_suffix(".csv.bak")
    shutil.copy2(authority_path, bak)

    auth_fieldnames, norm_to_real = _resolve_fieldnames(real_fieldnames, canonical_headers)
    remapped_entries = [
        {norm_to_real[_normalize_header(k)]: v for k, v in entry.items()}
        for entry in new_entries
    ]
    auth_rows.extend(remapped_entries)

    with open(authority_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=auth_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(auth_rows)

    return {"added": added, "skipped_existing": skipped_existing, "skipped_blocked": skipped_blocked}


def _load_new_rows(volumes: list[str], filename_suffix: str) -> list[dict]:
    """Loads {vol}_{filename_suffix} for every volume, tagging each row dict
    with its source volume. Missing files are simply skipped."""
    rows = []
    for vol in volumes:
        path = REVIEW_DIR / f"{vol}_{filename_suffix}"
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str).fillna("")
        for r in df.to_dict("records"):
            r["_volume"] = vol
            rows.append(r)
    return rows


def _person_duplicate_status(person_ids: set[str]) -> dict[str, tuple]:
    """{person_id: (status, detail)} for every id that appears in
    cross_volume_person_duplicates.csv as a_id or b_id. status is
    'blocked' if any matching row has decision=='same', else 'warning' if
    any matching row is unresolved (decision==''), else absent (no entry,
    meaning 'none')."""
    path = REVIEW_DIR / "cross_volume_person_duplicates.csv"
    result: dict[str, tuple] = {}
    if not path.exists():
        return result
    df = pd.read_csv(path, dtype=str).fillna("")
    for r in df.to_dict("records"):
        for this_id, other_id, other_name in (
            (r["a_id"], r["b_id"], r["b_name"]), (r["b_id"], r["a_id"], r["a_name"]),
        ):
            if this_id not in person_ids:
                continue
            decision = r.get("decision", "").strip().lower()
            if decision == "same":
                result[this_id] = ("blocked", f"Confirmed duplicate of {other_id} ({other_name})")
            elif this_id not in result:
                result[this_id] = ("warning", f"Unresolved possible duplicate of {other_id} ({other_name})")
    return result


def _place_duplicate_status(volume: str, place_ids: set[str]) -> dict[str, tuple]:
    """{place_id: ('warning', detail)} for every id referenced in this
    volume's nafnid candidates file. No 'confirmed same' signal exists for
    this tool today, so it is always 'warning', never 'blocked'."""
    path = REVIEW_DIR / f"{volume}_places_nafnid_candidates.csv"
    result: dict[str, tuple] = {}
    if not path.exists():
        return result
    df = pd.read_csv(path, dtype=str).fillna("")
    for r in df.to_dict("records"):
        pid = r.get("place_id", "")
        if pid in place_ids and pid not in result:
            result[pid] = ("warning", f"Unreviewed nafnid candidate: {r.get('candidate_name', '')}")
    return result


def aggregate_final_review(volumes: list[str]) -> list[dict]:
    """Everything currently review_status=='add' across every given volume's
    persons_new.csv/places_new.csv, not yet present in the authority file,
    annotated with duplicate_status ('none'|'warning'|'blocked') and
    duplicate_detail. Pure read -- safe to call on every UI render."""
    person_rows = [r for r in _load_new_rows(volumes, "persons_new.csv")
                   if r.get("review_status", "").strip().lower() == "add"]
    place_rows = [r for r in _load_new_rows(volumes, "places_new.csv")
                  if r.get("review_status", "").strip().lower() == "add"]

    person_auth = PersonAuthority()
    place_auth = PlaceAuthority()
    existing_person_ids = {e.person_id for e in person_auth.entries}
    existing_place_ids = {e.place_id for e in place_auth.entries}

    person_rows = [r for r in person_rows if r["person_id"] not in existing_person_ids]
    place_rows = [r for r in place_rows if r["place_id"] not in existing_place_ids]

    dup_status = _person_duplicate_status({r["person_id"] for r in person_rows})

    out = []
    for r in person_rows:
        status, detail = dup_status.get(r["person_id"], ("none", ""))
        out.append({
            "volume": r["_volume"], "entity_type": "person", "id": r["person_id"],
            "canonical_name": r.get("canonical_name", ""), "occupation": r.get("occupation", ""),
            "title": r.get("title", ""), "floruit_start": r.get("floruit_start", ""),
            "floruit_end": r.get("floruit_end", ""), "sources": r.get("sources", ""),
            "duplicate_status": status, "duplicate_detail": detail,
        })

    place_rows_by_vol: dict[str, list[dict]] = {}
    for r in place_rows:
        place_rows_by_vol.setdefault(r["_volume"], []).append(r)
    for vol, rows in place_rows_by_vol.items():
        dup_status_places = _place_duplicate_status(vol, {r["place_id"] for r in rows})
        for r in rows:
            status, detail = dup_status_places.get(r["place_id"], ("none", ""))
            out.append({
                "volume": vol, "entity_type": "place", "id": r["place_id"],
                "canonical_name": r.get("canonical_name", ""), "region": r.get("region", ""),
                "place_type": r.get("place_type", ""),
                "coordinates_lat": r.get("coordinates_lat", ""),
                "coordinates_long": r.get("coordinates_long", ""),
                "sources": r.get("sources", ""),
                "duplicate_status": status, "duplicate_detail": detail,
            })

    return out


def promote_persons(volumes: list[str], dry_run: bool = False) -> dict:
    rows = [r for r in _load_new_rows(volumes, "persons_new.csv")
            if r.get("review_status", "").strip().lower() == "add"]
    auth = PersonAuthority()
    existing_ids = {e.person_id for e in auth.entries}
    dup_status = _person_duplicate_status({r["person_id"] for r in rows})
    blocked_ids = {pid for pid, (status, _) in dup_status.items() if status == "blocked"}
    return _promote_rows(
        rows, blocked_ids, existing_ids, "person_id", _map_person_row,
        PERSON_AUTHORITY_PATH, PERSON_AUTHORITY_HEADERS, dry_run=dry_run,
    )


def promote_places(volumes: list[str], dry_run: bool = False) -> dict:
    rows = [r for r in _load_new_rows(volumes, "places_new.csv")
            if r.get("review_status", "").strip().lower() == "add"]
    auth = PlaceAuthority()
    existing_ids = {e.place_id for e in auth.entries}
    # No hard-block concept exists yet for places (see _place_duplicate_status).
    blocked_ids: set[str] = set()
    return _promote_rows(
        rows, blocked_ids, existing_ids, "place_id", _map_place_row,
        PLACE_AUTHORITY_PATH, PLACE_AUTHORITY_HEADERS, dry_run=dry_run,
    )


def promote_all(volumes: list[str], dry_run: bool = False) -> dict:
    """Recomputes eligibility itself at the moment of promotion (never
    trusts a possibly-stale list handed in from the UI -- this is the
    hard-block gate). Returns a combined summary."""
    persons_result = promote_persons(volumes, dry_run=dry_run)
    places_result = promote_places(volumes, dry_run=dry_run)
    return {"persons": persons_result, "places": places_result}
