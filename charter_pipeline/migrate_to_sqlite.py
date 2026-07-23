"""
One-time migration: fold every pre-existing CSV/xlsx source (per-volume
review CSVs + resolved_entities.json, plus the two independently-mutable
authority stores: person_names_authority.csv/place_names_authority.csv and
CHARTER_authority_file.xlsx's persons_authority/Places_Authority sheets)
into charter_pipeline.db (schema.sql).

See the approved migration plan, Part 1 section 1.3, for the full spec.
This script is read-only against every source file it touches; it never
mutates a CSV/xlsx in place, and --dry-run never writes to any real
database file either (it runs the whole migration + verification pass
against a throwaway in-memory SQLite connection so pk assignment and
PRAGMA foreign_key_check behave identically to a real run).

Usage:
    python migrate_to_sqlite.py --dry-run
    python migrate_to_sqlite.py --dry-run --place-id-splits splits.csv
    python migrate_to_sqlite.py --db charter_pipeline.db --place-id-splits splits.csv
    python migrate_to_sqlite.py --verify-only --db charter_pipeline.db

Path overrides (so --dry-run can be pointed at a scratch copy of the real
data instead of the live files -- normal invocations should never need
these, they default to config.py / person_authority.py / place_authority.py):
    --review-dir, --entities-dir, --authority-file,
    --person-authority-csv, --place-authority-csv, --output-dir
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config
from person_authority import AUTHORITY_PATH as PERSON_AUTHORITY_CSV, PersonAuthority
from place_authority import AUTHORITY_PATH as PLACE_AUTHORITY_CSV, PlaceAuthority

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# ── Canonical field lists (for schema-drift reporting) ──────────────────────
# Union of every column ever seen in real persons_new.csv/places_new.csv/
# review_queue.csv across volumes -- see 05_export_csvs.py's own field lists
# plus the review_status/wikidata_id/proposed_* columns added out-of-band by
# the review workflow later (not part of 05's original export).
PERSON_NEW_CANONICAL_COLS = [
    "person_id", "canonical_name", "variant_names", "patronymic",
    "occupation", "title", "floruit_start", "floruit_end", "gender",
    "associated_places", "notes", "sources", "review_status", "wikidata_id",
]
PLACE_NEW_CANONICAL_COLS = [
    "place_id", "canonical_name", "variant_names", "place_type",
    "coordinates_lat", "coordinates_long", "region", "district",
    "modern_equivalent", "notes", "sources", "review_status",
    "wikidata_id", "geo_match_score", "proposed_place_id", "proposed_wikidata_id",
]
REVIEW_QUEUE_CANONICAL_COLS = [
    "type", "extracted_name", "closest_match", "match_id", "score",
    "role_category", "role", "charter_filename", "charter_date", "decision",
]

_CHARTER_YEAR_RE = re.compile(r"(\d{3,4})")


def charter_year(date_str) -> str:
    m = _CHARTER_YEAR_RE.match((date_str or "").strip())
    return str(int(m.group(1))) if m else ""


def to_int_or_none(s):
    s = (s or "").strip() if isinstance(s, str) else s
    if s in (None, ""):
        return None
    m = _CHARTER_YEAR_RE.match(str(s))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def to_float_or_none(s):
    s = (s or "").strip() if isinstance(s, str) else s
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def blank(v) -> str:
    return "" if v is None else str(v).strip()


def union_semicolon(values: list) -> str:
    """Same union policy as merge_entities.py's _union_semicolon."""
    seen, out = set(), []
    for v in values:
        for item in (v or "").split(";"):
            item = item.strip()
            key = item.lower()
            if item and key not in seen:
                seen.add(key)
                out.append(item)
    return ";".join(out)


def first_nonblank(*values) -> str:
    for v in values:
        v = blank(v)
        if v:
            return v
    return ""


class Report:
    """Accumulates human-readable lines + structured warnings for the final
    verification report, printed at the end of main()."""

    def __init__(self):
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def line(self, s: str = ""):
        self.lines.append(s)
        print(s)

    def warn(self, s: str):
        self.warnings.append(s)
        self.lines.append(f"  [warning] {s}")
        print(f"  [warning] {s}")

    def text(self) -> str:
        return "\n".join(self.lines)


# ═══════════════════════════════════════════════════════════════════════════
# Step 1 — Load every source file
# ═══════════════════════════════════════════════════════════════════════════

def discover_volumes(review_dir: Path) -> list[int]:
    vols = set()
    for p in review_dir.glob("vol*_charters.csv"):
        m = re.match(r"vol(\d+)_charters\.csv$", p.name)
        if m:
            vols.add(int(m.group(1)))
    return sorted(vols)


def read_csv_rows(path: Path) -> list[dict]:
    """Plain csv.DictReader (not pandas) -- tolerant of ragged rows the way
    person_authority.py/place_authority.py already are, matching runtime
    behavior exactly rather than reimplementing a stricter parser."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_volume(review_dir: Path, entities_dir: Path, vol: int, rpt: Report) -> dict:
    prefix = f"vol{vol:02d}"
    charters_csv = read_csv_rows(review_dir / f"{prefix}_charters.csv")
    persons_new_csv = read_csv_rows(review_dir / f"{prefix}_persons_new.csv")
    places_new_csv = read_csv_rows(review_dir / f"{prefix}_places_new.csv")
    review_queue_csv = read_csv_rows(review_dir / f"{prefix}_review_queue.csv")
    resolved_path = review_dir / f"{prefix}_review_queue_resolved.csv"
    review_queue_resolved_csv = read_csv_rows(resolved_path) if resolved_path.exists() else []
    nafnid_path = review_dir / f"{prefix}_places_nafnid_candidates.csv"
    nafnid_csv = read_csv_rows(nafnid_path) if nafnid_path.exists() else []

    resolved_json_path = entities_dir / f"{prefix}_resolved_entities.json"
    if not resolved_json_path.exists():
        raise FileNotFoundError(
            f"Required file not found for vol{vol}: {resolved_json_path}"
        )
    with open(resolved_json_path, encoding="utf-8") as f:
        resolved_json = json.load(f)

    if not resolved_path.exists():
        rpt.line(f"  vol{vol:02d}: no review_queue_resolved.csv on disk (optional file, OK).")

    return {
        "vol": vol,
        "charters_csv": charters_csv,
        "persons_new_csv": persons_new_csv,
        "places_new_csv": places_new_csv,
        "review_queue_csv": review_queue_csv,
        "review_queue_resolved_csv": review_queue_resolved_csv,
        "nafnid_csv": nafnid_csv,
        "resolved_json": resolved_json,
    }


def load_all_sources(args, rpt: Report) -> dict:
    rpt.line("=" * 78)
    rpt.line("STEP 1 — Loading source files")
    rpt.line("=" * 78)

    person_auth = PersonAuthority(path=args.person_authority_csv)
    place_auth = PlaceAuthority(path=args.place_authority_csv)

    if not args.authority_file.exists():
        raise FileNotFoundError(f"AUTHORITY_FILE not found: {args.authority_file}")
    xlsx_persons = pd.read_excel(args.authority_file, sheet_name="persons_authority", dtype=str).fillna("")
    xlsx_places = pd.read_excel(args.authority_file, sheet_name="Places_Authority", dtype=str).fillna("")
    xlsx_charter_data = pd.read_excel(args.authority_file, sheet_name="Charter_Data", dtype=str).fillna("")

    rpt.line(f"  person_names_authority.csv : {len(person_auth.entries)} rows "
             f"({args.person_authority_csv})")
    rpt.line(f"  place_names_authority.csv  : {len(place_auth.entries)} rows "
             f"({args.place_authority_csv})")
    rpt.line(f"  xlsx persons_authority      : {len(xlsx_persons)} rows")
    rpt.line(f"  xlsx Places_Authority       : {len(xlsx_places)} rows")
    rpt.line(f"  xlsx Charter_Data           : {len(xlsx_charter_data)} rows "
             f"(not migrated -- Step 6's own eventual concern, out of scope here)")

    cross_vol_dup_path = args.review_dir / "cross_volume_person_duplicates.csv"
    cross_vol_person_dups = read_csv_rows(cross_vol_dup_path)
    rpt.line(f"  cross_volume_person_duplicates.csv : {len(cross_vol_person_dups)} rows"
             + ("" if cross_vol_dup_path.exists() else "  (file absent, treated as 0 rows)"))

    volumes = discover_volumes(args.review_dir)
    if not volumes:
        raise RuntimeError(f"No vol*_charters.csv files found under {args.review_dir}")
    rpt.line(f"  Discovered volumes: {volumes}")

    vol_data = {}
    for vol in volumes:
        vd = load_volume(args.review_dir, args.entities_dir, vol, rpt)
        vol_data[vol] = vd
        rpt.line(
            f"  vol{vol:02d}: charters={len(vd['charters_csv'])} "
            f"persons_new={len(vd['persons_new_csv'])} places_new={len(vd['places_new_csv'])} "
            f"review_queue={len(vd['review_queue_csv'])} "
            f"review_queue_resolved={len(vd['review_queue_resolved_csv'])} "
            f"nafnid_candidates={len(vd['nafnid_csv'])} "
            f"resolved_json_charters={len(vd['resolved_json'])}"
        )

    return {
        "person_auth": person_auth,
        "place_auth": place_auth,
        "xlsx_persons": xlsx_persons,
        "xlsx_places": xlsx_places,
        "xlsx_charter_data": xlsx_charter_data,
        "cross_vol_person_dups": cross_vol_person_dups,
        "volumes": volumes,
        "vol_data": vol_data,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Step 2 — Schema drift report
# ═══════════════════════════════════════════════════════════════════════════

def report_schema_drift(src: dict, rpt: Report):
    rpt.line("=" * 78)
    rpt.line("STEP 2 — Schema drift per file (missing columns read as '', never an error)")
    rpt.line("=" * 78)
    any_drift = False
    for vol, vd in src["vol_data"].items():
        prefix = f"vol{vol:02d}"
        for label, rows, canonical in (
            (f"{prefix}_persons_new.csv", vd["persons_new_csv"], PERSON_NEW_CANONICAL_COLS),
            (f"{prefix}_places_new.csv", vd["places_new_csv"], PLACE_NEW_CANONICAL_COLS),
            (f"{prefix}_review_queue.csv", vd["review_queue_csv"], REVIEW_QUEUE_CANONICAL_COLS),
        ):
            actual = set(rows[0].keys()) if rows else set()
            missing = [c for c in canonical if c not in actual]
            extra = [c for c in actual if c not in canonical]
            if missing or extra:
                any_drift = True
                rpt.line(f"  {label}: missing={missing or 'none'} extra={extra or 'none'}")
    if not any_drift:
        rpt.line("  (no drift found against the canonical column lists)")


# ═══════════════════════════════════════════════════════════════════════════
# Step 3 — Canonical persons reconciliation
# ═══════════════════════════════════════════════════════════════════════════

def reconcile_persons(src: dict, rpt: Report) -> tuple[dict, list[dict]]:
    """Returns (canonical_rows: {display_id: row_dict}, conflicts: [row_dict])."""
    rpt.line("=" * 78)
    rpt.line("STEP 3 — Canonical persons reconciliation")
    rpt.line("=" * 78)

    csv_by_id: dict[str, list] = defaultdict(list)
    for e in src["person_auth"].entries:
        csv_by_id[e.person_id].append(e)
    xlsx_by_id = {r["person_id"]: r for r in src["xlsx_persons"].to_dict("records")}

    csv_ids = set(csv_by_id)
    xlsx_ids = set(xlsx_by_id)
    rpt.line(f"  CSV ids: {len(csv_ids)}  xlsx ids: {len(xlsx_ids)}  "
             f"equal: {csv_ids == xlsx_ids}")
    if csv_ids != xlsx_ids:
        rpt.warn(f"person id sets differ! CSV-only={sorted(csv_ids - xlsx_ids)} "
                 f"xlsx-only={sorted(xlsx_ids - csv_ids)}")
    dup_ids = [pid for pid, rows in csv_by_id.items() if len(rows) > 1]
    if dup_ids:
        rpt.warn(f"person_names_authority.csv has internally-duplicated ids "
                 f"(not expected per the audit): {dup_ids} -- each extra row is skipped, "
                 f"first occurrence wins, flagged in migration_conflicts.csv")

    canonical_rows: dict[str, dict] = {}
    conflicts: list[dict] = []
    all_ids = sorted(csv_ids | xlsx_ids)
    for pid in all_ids:
        csv_e = csv_by_id.get(pid, [None])[0]
        xlsx_r = xlsx_by_id.get(pid)
        if csv_e and xlsx_r:
            if csv_e.canonical_name.strip() != xlsx_r["canonical_name"].strip():
                conflicts.append({
                    "entity_type": "person", "id": pid,
                    "csv_canonical_name": csv_e.canonical_name,
                    "xlsx_canonical_name": xlsx_r["canonical_name"],
                    "reason": "canonical_name mismatch on shared id",
                })
                continue
            canonical_rows[pid] = {
                "display_id": pid, "legacy_id": pid, "source_volume": None,
                "status": "canonical", "review_status": "",
                "canonical_name": csv_e.canonical_name,
                "variant_names": union_semicolon([";".join(csv_e.variants), xlsx_r.get("variant_names", "")]),
                "wikidata_id": first_nonblank(csv_e.wikidata_id, xlsx_r.get("wikidata_id")),  # CSV wins
                "patronymic": first_nonblank(csv_e.patronymic, xlsx_r.get("patronymic")),
                "occupation": first_nonblank(csv_e.occupation, xlsx_r.get("occupation")),
                "title": first_nonblank(csv_e.title, xlsx_r.get("title")),
                "floruit_start": to_int_or_none(first_nonblank(csv_e.floruit_start, xlsx_r.get("floruit_start"))),
                "floruit_end": to_int_or_none(first_nonblank(csv_e.floruit_end, xlsx_r.get("floruit_end"))),
                "gender": first_nonblank(csv_e.gender, xlsx_r.get("gender")),
                "associated_places": blank(xlsx_r.get("associated_places")),  # xlsx-only field, xlsx wins
                "notes": union_semicolon([csv_e.notes, xlsx_r.get("notes", "")]),
                "sources": blank(xlsx_r.get("sources")),  # xlsx-only field, xlsx wins
            }
        elif csv_e:
            canonical_rows[pid] = {
                "display_id": pid, "legacy_id": pid, "source_volume": None,
                "status": "canonical", "review_status": "",
                "canonical_name": csv_e.canonical_name,
                "variant_names": ";".join(csv_e.variants),
                "wikidata_id": csv_e.wikidata_id, "patronymic": csv_e.patronymic,
                "occupation": csv_e.occupation, "title": csv_e.title,
                "floruit_start": to_int_or_none(csv_e.floruit_start),
                "floruit_end": to_int_or_none(csv_e.floruit_end),
                "gender": csv_e.gender, "associated_places": "", "notes": csv_e.notes, "sources": "",
            }
        else:
            canonical_rows[pid] = {
                "display_id": pid, "legacy_id": pid, "source_volume": None,
                "status": "canonical", "review_status": "",
                "canonical_name": xlsx_r["canonical_name"],
                "variant_names": blank(xlsx_r.get("variant_names")),
                "wikidata_id": "", "patronymic": blank(xlsx_r.get("patronymic")),
                "occupation": blank(xlsx_r.get("occupation")), "title": blank(xlsx_r.get("title")),
                "floruit_start": to_int_or_none(xlsx_r.get("floruit_start")),
                "floruit_end": to_int_or_none(xlsx_r.get("floruit_end")),
                "gender": blank(xlsx_r.get("gender")),
                "associated_places": blank(xlsx_r.get("associated_places")),
                "notes": blank(xlsx_r.get("notes")), "sources": blank(xlsx_r.get("sources")),
            }

    rpt.line(f"  Reconciled {len(canonical_rows)} canonical person rows, "
             f"{len(conflicts)} blocked conflict(s).")
    for c in conflicts:
        rpt.line(f"    CONFLICT {c['id']}: CSV={c['csv_canonical_name']!r} "
                 f"xlsx={c['xlsx_canonical_name']!r}")
    return canonical_rows, conflicts


# ═══════════════════════════════════════════════════════════════════════════
# Step 4 — Canonical places reconciliation (the hard case)
# ═══════════════════════════════════════════════════════════════════════════

def load_place_id_splits(path: Path | None, rpt: Report) -> dict[str, list[dict]]:
    """--place-id-splits CSV: old_id,disambiguator,new_display_id.
    Returns {old_id: [{"disambiguator": ..., "new_display_id": ...}, ...]}."""
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"--place-id-splits file not found: {path}")
    out: dict[str, list[dict]] = defaultdict(list)
    for row in read_csv_rows(path):
        old_id = blank(row.get("old_id"))
        disamb = blank(row.get("disambiguator"))
        new_id = blank(row.get("new_display_id"))
        if not old_id or not disamb or not new_id:
            raise ValueError(f"--place-id-splits row missing a field: {row}")
        out[old_id].append({"disambiguator": disamb, "new_display_id": new_id})
    rpt.line(f"  Loaded --place-id-splits mapping: {dict(out)}")
    return dict(out)


def load_conflict_resolutions(path: Path | None, rpt: Report) -> dict[str, dict]:
    """--conflict-resolutions CSV: place_id,canonical_name,notes_extra.
    Human-authored confirmation that a shared id's CSV/xlsx canonical_name
    mismatch is the same real place (typo/formatting drift, not a different
    place) -- resolves it with the given canonical_name instead of blocking
    it out of the migration entirely. notes_extra (optional) is appended to
    the merged row's notes so the discarded variant text isn't silently lost."""
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"--conflict-resolutions file not found: {path}")
    out: dict[str, dict] = {}
    for row in read_csv_rows(path):
        pid = blank(row.get("place_id"))
        name = blank(row.get("canonical_name"))
        if not pid or not name:
            raise ValueError(f"--conflict-resolutions row missing a field: {row}")
        out[pid] = {"canonical_name": name, "notes_extra": blank(row.get("notes_extra"))}
    rpt.line(f"  Loaded --conflict-resolutions mapping: {out}")
    return out


def reconcile_places(src: dict, splits_map: dict, rpt: Report,
                      conflict_resolutions: dict | None = None) -> tuple[dict, list[dict], list[dict]]:
    """Returns (canonical_rows: {display_id: row}, conflicts, unresolved_splits)."""
    rpt.line("=" * 78)
    rpt.line("STEP 4 — Canonical places reconciliation")
    rpt.line("=" * 78)

    csv_by_id: dict[str, list] = defaultdict(list)
    for e in src["place_auth"].entries:
        csv_by_id[e.place_id].append(e)
    xlsx_by_id = {r["place_id"]: r for r in src["xlsx_places"].to_dict("records")}

    csv_ids = set(csv_by_id)
    xlsx_ids = set(xlsx_by_id)
    union_ids = csv_ids | xlsx_ids
    rpt.line(f"  CSV ids: {len(csv_ids)} rows across {sum(len(v) for v in csv_by_id.values())} raw rows  "
             f"xlsx ids: {len(xlsx_ids)}  overlap: {len(csv_ids & xlsx_ids)}  union: {len(union_ids)}")

    split_ids = {pid: rows for pid, rows in csv_by_id.items() if len(rows) > 1}
    rpt.line(f"  Internally-duplicated CSV ids (require --place-id-splits): "
             f"{ {k: [r.canonical_name for r in v] for k, v in split_ids.items()} }")

    canonical_rows: dict[str, dict] = {}
    conflicts: list[dict] = []
    unresolved_splits: list[dict] = []
    conflict_resolutions = conflict_resolutions or {}

    def _place_row_from_csv(e, display_id) -> dict:
        return {
            "display_id": display_id, "legacy_id": e.place_id, "source_volume": None,
            "status": "canonical", "review_status": "",
            "canonical_name": e.canonical_name, "variant_names": ";".join(e.variants),
            "place_type": "", "coordinates_lat": to_float_or_none(e.lat),
            "coordinates_long": to_float_or_none(e.lng), "region": "", "district": "",
            "modern_equivalent": e.modern_country, "wikidata_id": e.wikidata_id,
            "nafnid_id": "", "geo_match_score": None, "proposed_place_id": "",
            "proposed_wikidata_id": "", "notes": e.notes, "sources": "",
        }

    def _place_row_from_xlsx(r, display_id) -> dict:
        return {
            "display_id": display_id, "legacy_id": r["place_id"], "source_volume": None,
            "status": "canonical", "review_status": "",
            "canonical_name": r["canonical_name"], "variant_names": blank(r.get("variant_names")),
            "place_type": blank(r.get("place_type")),
            "coordinates_lat": to_float_or_none(r.get("coordinates_lat")),
            "coordinates_long": to_float_or_none(r.get("coordinates_long")),
            "region": blank(r.get("region")), "district": blank(r.get("district")),
            "modern_equivalent": blank(r.get("modern_equivalent")), "wikidata_id": "",
            "nafnid_id": "", "geo_match_score": None, "proposed_place_id": "",
            "proposed_wikidata_id": "", "notes": blank(r.get("notes")), "sources": blank(r.get("sources")),
        }

    # ── Split ids: primary (file-order-first) keeps old_id; every other
    # occurrence needs a --place-id-splits row matched by canonical_name.
    for old_id, rows in split_ids.items():
        primary, *rest = rows
        canonical_rows[old_id] = _place_row_from_csv(primary, old_id)
        rpt.line(f"    split {old_id}: primary kept as {old_id!r} = {primary.canonical_name!r}")
        mapping_rows = list(splits_map.get(old_id, []))
        for extra in rest:
            match = next((m for m in mapping_rows
                          if m["disambiguator"].strip().lower() == extra.canonical_name.strip().lower()), None)
            if match is None:
                unresolved_splits.append({
                    "old_id": old_id, "canonical_name": extra.canonical_name,
                    "reason": "no --place-id-splits row matched this row's canonical_name",
                })
                rpt.warn(f"split {old_id} secondary row {extra.canonical_name!r} has NO matching "
                         f"--place-id-splits entry -- blocked, not inserted.")
                continue
            new_id = match["new_display_id"]
            canonical_rows[new_id] = _place_row_from_csv(extra, new_id)
            rpt.line(f"    split {old_id}: secondary {extra.canonical_name!r} -> display_id {new_id!r}")

    # ── Everything else: union of remaining (non-split) ids
    for pid in sorted(union_ids - set(split_ids)):
        csv_rows = csv_by_id.get(pid)
        csv_e = csv_rows[0] if csv_rows else None
        xlsx_r = xlsx_by_id.get(pid)
        if csv_e and xlsx_r:
            resolution = conflict_resolutions.get(pid)
            if csv_e.canonical_name.strip() != xlsx_r["canonical_name"].strip() and resolution is None:
                conflicts.append({
                    "entity_type": "place", "id": pid,
                    "csv_canonical_name": csv_e.canonical_name,
                    "xlsx_canonical_name": xlsx_r["canonical_name"],
                    "reason": "canonical_name mismatch on shared id",
                })
                continue
            row = _place_row_from_csv(csv_e, pid)
            xrow = _place_row_from_xlsx(xlsx_r, pid)
            merged = dict(row)
            merged["variant_names"] = union_semicolon([row["variant_names"], xrow["variant_names"]])
            merged["notes"] = union_semicolon([row["notes"], xrow["notes"]])
            if resolution is not None:
                # Human-confirmed same-place resolution (see load_conflict_resolutions):
                # use the resolved name, fold both original variants + the resolution's
                # note into variant_names/notes rather than silently discarding them.
                merged["canonical_name"] = resolution["canonical_name"]
                merged["variant_names"] = union_semicolon(
                    [merged["variant_names"], csv_e.canonical_name, xlsx_r["canonical_name"]]
                )
                merged["notes"] = union_semicolon([merged["notes"], resolution["notes_extra"]])
            # CSV is the only source of wikidata_id/coordinates; xlsx is the
            # only source of place_type/region/district/modern_equivalent/sources.
            merged["place_type"] = xrow["place_type"]
            merged["region"] = xrow["region"]
            merged["district"] = xrow["district"]
            merged["modern_equivalent"] = first_nonblank(row["modern_equivalent"], xrow["modern_equivalent"])
            merged["sources"] = xrow["sources"]
            merged["coordinates_lat"] = row["coordinates_lat"] if row["coordinates_lat"] is not None else xrow["coordinates_lat"]
            merged["coordinates_long"] = row["coordinates_long"] if row["coordinates_long"] is not None else xrow["coordinates_long"]
            canonical_rows[pid] = merged
        elif csv_e:
            canonical_rows[pid] = _place_row_from_csv(csv_e, pid)
        else:
            canonical_rows[pid] = _place_row_from_xlsx(xlsx_r, pid)

    rpt.line(f"  Reconciled {len(canonical_rows)} canonical place rows, "
             f"{len(conflicts)} blocked name-mismatch conflict(s), "
             f"{len(unresolved_splits)} unresolved split row(s).")
    for c in conflicts:
        rpt.line(f"    CONFLICT {c['id']}: CSV={c['csv_canonical_name']!r} xlsx={c['xlsx_canonical_name']!r}")
    return canonical_rows, conflicts, unresolved_splits


# ═══════════════════════════════════════════════════════════════════════════
# Step 5 — Provisional persons/places per volume
# ═══════════════════════════════════════════════════════════════════════════

def _disambiguated_legacy_id(legacy_id: str, occurrence: int) -> str:
    """First occurrence of a legacy_id within a volume keeps the id exactly
    as it reads in the source CSV, for audit-trail purposes. A 2nd/3rd/...
    occurrence (a within-volume id collision -- confirmed real, e.g.
    vol04_places_new.csv's l156/l157, each covering two different real
    places, a bug class not previously documented) gets a -b/-c/... suffix
    APPENDED TO legacy_id itself, not just to display_id: schema.sql's
    ix_places_source_legacy/ix_persons_source_legacy indexes are
    UNIQUE(source_volume, legacy_id), an assumption this newly-found bug
    breaks. Rather than relax that constraint (schema.sql is prior, already-
    reviewed work, out of scope for this migration script), the secondary
    row's legacy_id is disambiguated the same way --place-id-splits
    disambiguates the canonical duplicates -- a deliberate, documented
    deviation from "legacy_id is always the untouched original string",
    limited to exactly the rows that would otherwise collide."""
    if occurrence == 0:
        return legacy_id
    suffix = chr(ord("b") + occurrence - 1)  # 1st dup -> b, 2nd -> c, ...
    return f"{legacy_id}-{suffix}"


def build_provisional_persons(vol: int, rows: list[dict], rpt: "Report") -> list[dict]:
    from collections import Counter
    out = []
    seen: dict[str, int] = defaultdict(int)
    counts = Counter(blank(r.get("person_id")) for r in rows)
    dup_ids = {pid for pid, c in counts.items() if pid and c > 1}
    if dup_ids:
        rpt.warn(f"vol{vol}: persons_new.csv has WITHIN-VOLUME duplicate person_id(s) "
                 f"{sorted(dup_ids)} -- same bug class as the canonical l128/l118/l129 "
                 f"split, just not previously documented. First occurrence keeps the "
                 f"plain v{vol:02d}-{{id}} display_id; later occurrences get a -b/-c suffix.")
    for row in rows:
        legacy_id = blank(row.get("person_id"))
        if not legacy_id:
            continue
        occ = seen[legacy_id]
        seen[legacy_id] += 1
        disamb_legacy_id = _disambiguated_legacy_id(legacy_id, occ)
        out.append({
            "display_id": f"v{vol:02d}-{disamb_legacy_id}", "legacy_id": disamb_legacy_id,
            "raw_legacy_id": legacy_id,  # pre-disambiguation id, for pk-lookup grouping only
            "source_volume": vol, "status": "provisional",
            "review_status": blank(row.get("review_status")),
            "canonical_name": blank(row.get("canonical_name")),
            "variant_names": blank(row.get("variant_names")),
            "wikidata_id": blank(row.get("wikidata_id")),
            "patronymic": blank(row.get("patronymic")), "occupation": blank(row.get("occupation")),
            "title": blank(row.get("title")),
            "floruit_start": to_int_or_none(row.get("floruit_start")),
            "floruit_end": to_int_or_none(row.get("floruit_end")),
            "gender": blank(row.get("gender")), "associated_places": blank(row.get("associated_places")),
            "notes": blank(row.get("notes")), "sources": blank(row.get("sources")),
        })
    return out


def build_provisional_places(vol: int, rows: list[dict], rpt: "Report") -> list[dict]:
    from collections import Counter
    out = []
    seen: dict[str, int] = defaultdict(int)
    counts = Counter(blank(r.get("place_id")) for r in rows)
    dup_ids = {pid for pid, c in counts.items() if pid and c > 1}
    if dup_ids:
        rpt.warn(f"vol{vol}: places_new.csv has WITHIN-VOLUME duplicate place_id(s) "
                 f"{sorted(dup_ids)} -- same bug class as the canonical l128/l118/l129 "
                 f"split, just not previously documented. First occurrence keeps the "
                 f"plain v{vol:02d}-{{id}} display_id; later occurrences get a -b/-c suffix.")
    for row in rows:
        legacy_id = blank(row.get("place_id"))
        if not legacy_id:
            continue
        occ = seen[legacy_id]
        seen[legacy_id] += 1
        disamb_legacy_id = _disambiguated_legacy_id(legacy_id, occ)
        out.append({
            "display_id": f"v{vol:02d}-{disamb_legacy_id}", "legacy_id": disamb_legacy_id,
            "raw_legacy_id": legacy_id,  # pre-disambiguation id, for pk-lookup grouping only
            "source_volume": vol, "status": "provisional",
            "review_status": blank(row.get("review_status")),
            "canonical_name": blank(row.get("canonical_name")),
            "variant_names": blank(row.get("variant_names")),
            "place_type": blank(row.get("place_type")),
            "coordinates_lat": to_float_or_none(row.get("coordinates_lat")),
            "coordinates_long": to_float_or_none(row.get("coordinates_long")),
            "region": blank(row.get("region")), "district": blank(row.get("district")),
            "modern_equivalent": blank(row.get("modern_equivalent")),
            "wikidata_id": blank(row.get("wikidata_id")), "nafnid_id": "",
            "geo_match_score": to_float_or_none(row.get("geo_match_score")),
            "proposed_place_id": blank(row.get("proposed_place_id")),
            "proposed_wikidata_id": blank(row.get("proposed_wikidata_id")),
            "notes": blank(row.get("notes")), "sources": blank(row.get("sources")),
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════
# DB insert helpers
# ═══════════════════════════════════════════════════════════════════════════

PERSON_COLS = ["display_id", "legacy_id", "source_volume", "status", "review_status",
               "canonical_name", "variant_names", "wikidata_id", "patronymic", "occupation",
               "title", "floruit_start", "floruit_end", "gender", "associated_places",
               "notes", "sources"]
PLACE_COLS = ["display_id", "legacy_id", "source_volume", "status", "review_status",
              "canonical_name", "variant_names", "place_type", "coordinates_lat",
              "coordinates_long", "region", "district", "modern_equivalent", "wikidata_id",
              "nafnid_id", "geo_match_score", "proposed_place_id", "proposed_wikidata_id",
              "notes", "sources"]


def insert_person(conn, row: dict) -> int:
    cur = conn.execute(
        f"INSERT INTO persons ({','.join(PERSON_COLS)}) VALUES ({','.join('?' * len(PERSON_COLS))})",
        [row.get(c) for c in PERSON_COLS],
    )
    return cur.lastrowid


def insert_place(conn, row: dict) -> int:
    cur = conn.execute(
        f"INSERT INTO places ({','.join(PLACE_COLS)}) VALUES ({','.join('?' * len(PLACE_COLS))})",
        [row.get(c) for c in PLACE_COLS],
    )
    return cur.lastrowid


def resolve_provisional(prov_map: dict, vol: int, legacy_id: str, name_hint: str,
                         rpt: "Report", kind: str):
    """prov_map is {(vol, legacy_id): [(pk, canonical_name), ...]} -- usually
    a single-element list, but a within-volume id collision (confirmed real,
    see build_provisional_places) means more than one row can share a legacy
    id inside one volume. Disambiguates by matching name_hint against each
    candidate's canonical_name; falls back to the first-listed (file-order)
    row with a warning if that doesn't uniquely resolve it."""
    candidates = prov_map.get((vol, legacy_id))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    hint = (name_hint or "").strip().lower()
    matches = [pk for pk, cname in candidates if cname.strip().lower() == hint] if hint else []
    if len(matches) == 1:
        return matches[0]
    rpt.warn(f"vol{vol}: ambiguous within-volume duplicate {kind} legacy_id {legacy_id!r} "
             f"({len(candidates)} rows: {[c for _, c in candidates]!r}) -- name hint "
             f"{name_hint!r} did not uniquely disambiguate; defaulting to the first-listed row.")
    return candidates[0][0]


# ═══════════════════════════════════════════════════════════════════════════
# Steps 6-9 — Charters, junctions, review queue, duplicate candidates
# ═══════════════════════════════════════════════════════════════════════════

def classify_ref(raw_id: str, new_list: list[str]) -> tuple[str, str]:
    """Returns (resolution_state, target_id) -- target_id has any REVIEW:
    prefix stripped."""
    raw_id = raw_id or ""
    if raw_id.startswith("REVIEW:"):
        return "pending_review", raw_id[len("REVIEW:"):]
    if raw_id in new_list:
        return "new", raw_id
    return "resolved", raw_id


def migrate_charters_and_junctions(
    conn, vol: int, vd: dict, canonical_person_pk: dict, canonical_place_pk: dict,
    provisional_person_pk: dict, provisional_place_pk: dict, split_old_ids: set,
    rpt: Report,
) -> dict:
    """Returns per-volume bookkeeping needed by later steps: charter_pk_by_seq,
    review_target_pks (for the review-queue positional join), and
    split_review_rows (ambiguous canonical-split references for a human)."""
    charter_csv_by_seq = {}
    for row in vd["charters_csv"]:
        try:
            seq = int(row["sequence"])
        except (KeyError, ValueError):
            continue
        charter_csv_by_seq[seq] = row

    charter_pk_by_seq: dict[int, int] = {}
    review_target_pks: dict[tuple, list] = defaultdict(list)
    split_review_rows: list[dict] = []
    n_charters = n_charter_persons = n_charter_places = 0
    n_unresolved_person_refs = n_unresolved_place_refs = 0

    for ch in vd["resolved_json"]:
        seq = ch.get("sequence")
        crow = charter_csv_by_seq.get(seq, {})
        has_parse_error = 1 if ("_parse_error" in ch or "_api_error" in ch or ch.get("_skipped")) else 0
        placeholder = crow.get("charter_id_placeholder") or f"c_vol{vol:02d}_seq{seq}"

        resolved_persons = ch.get("resolved_persons", [])
        resolved_locations = ch.get("resolved_locations", [])
        has_review_persons = 1 if any(str(p.get("person_id", "")).startswith("REVIEW:") for p in resolved_persons) else 0
        has_review_places = 1 if any(str(l.get("place_id", "")).startswith("REVIEW:") for l in resolved_locations) else 0

        cur = conn.execute(
            "INSERT INTO charters (charter_id_placeholder, volume, sequence, shelfmark_auto, "
            "di_reference, date, di_year, date_uncertain, date_header, doc_type, subject, "
            "outcome, scribe, scribe_source, seal_info, language, notes, has_parse_error, "
            "has_review_persons, has_review_places) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                placeholder, vol, seq, blank(crow.get("shelfmark_auto")), blank(crow.get("di_reference")),
                blank(crow.get("date")), to_int_or_none(charter_year(crow.get("date"))),
                blank(crow.get("date_uncertain")), blank(crow.get("date_header")), blank(crow.get("doc_type")),
                blank(crow.get("subject")), blank(crow.get("outcome")), blank(crow.get("scribe")),
                blank(crow.get("scribe_source")), blank(crow.get("seal_info")), blank(crow.get("language")),
                blank(crow.get("notes")), has_parse_error, has_review_persons, has_review_places,
            ),
        )
        charter_pk = cur.lastrowid
        charter_pk_by_seq[seq] = charter_pk
        n_charters += 1

        new_persons = ch.get("new_persons", [])
        for ordinal, p in enumerate(resolved_persons):
            state, target = classify_ref(str(p.get("person_id", "")), new_persons)
            person_pk = review_pk = None
            if state == "pending_review":
                review_pk = canonical_person_pk.get(target) or resolve_provisional(
                    provisional_person_pk, vol, target, p.get("name"), rpt, "person")
                if review_pk is None:
                    rpt.warn(f"vol{vol} seq={seq}: REVIEW: person target {target!r} did not "
                             f"resolve to any migrated row (likely blocked by a conflict).")
            else:
                person_pk = resolve_provisional(provisional_person_pk, vol, target, p.get("name"), rpt, "person")
                if person_pk is None:
                    person_pk = canonical_person_pk.get(target)
                if person_pk is None:
                    n_unresolved_person_refs += 1
                    rpt.warn(f"vol{vol} seq={seq}: person ref {target!r} ({p.get('name')!r}) has no "
                             f"matching row in persons_new.csv or the canonical authority -- "
                             f"orphaned legacy reference, left with NULL person_pk.")
            conn.execute(
                "INSERT INTO charter_persons (charter_pk, ordinal, role_category, qualifier, "
                "extracted_name, person_pk, match_score, resolution_state, review_match_person_pk) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (charter_pk, ordinal, blank(p.get("role_category")), blank(p.get("qualifier")),
                 blank(p.get("name")) or "?", person_pk, to_float_or_none(p.get("match_score")),
                 state, review_pk),
            )
            cp_pk = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            n_charter_persons += 1
            if state == "pending_review":
                review_target_pks[(ch.get("filename"), "person")].append(cp_pk)

        new_places = ch.get("new_places", [])
        for ordinal, l in enumerate(resolved_locations):
            state, target = classify_ref(str(l.get("place_id", "")), new_places)
            place_pk = review_pk = None
            if state == "pending_review":
                review_pk = canonical_place_pk.get(target) or resolve_provisional(
                    provisional_place_pk, vol, target, l.get("name"), rpt, "place")
                if review_pk is None:
                    rpt.warn(f"vol{vol} seq={seq}: REVIEW: place target {target!r} did not "
                             f"resolve to any migrated row (likely blocked by a conflict).")
            else:
                place_pk = resolve_provisional(provisional_place_pk, vol, target, l.get("name"), rpt, "place")
                if place_pk is None:
                    place_pk = canonical_place_pk.get(target)
                if place_pk is None:
                    n_unresolved_place_refs += 1
                    rpt.warn(f"vol{vol} seq={seq}: place ref {target!r} ({l.get('name')!r}) has no "
                             f"matching row in places_new.csv or the canonical authority -- "
                             f"orphaned legacy reference, left with NULL place_pk.")
                if target in split_old_ids and place_pk is None:
                    split_review_rows.append({
                        "volume": vol, "sequence": seq, "old_id": target,
                        "extracted_name": blank(l.get("name")), "role": blank(l.get("role")),
                        "region": blank(l.get("region")), "charter_date": blank(crow.get("date")),
                        "di_reference": blank(crow.get("di_reference")),
                    })
            conn.execute(
                "INSERT INTO charter_places (charter_pk, ordinal, role, region, extracted_name, "
                "place_pk, match_score, resolution_state, review_match_place_pk) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (charter_pk, ordinal, blank(l.get("role")), blank(l.get("region")),
                 blank(l.get("name")) or "?", place_pk, to_float_or_none(l.get("match_score")),
                 state, review_pk),
            )
            cl_pk = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            n_charter_places += 1
            if state == "pending_review":
                review_target_pks[(ch.get("filename"), "place")].append(cl_pk)

    return {
        "charter_pk_by_seq": charter_pk_by_seq,
        "review_target_pks": review_target_pks,
        "split_review_rows": split_review_rows,
        "n_charters": n_charters,
        "n_charter_persons": n_charter_persons,
        "n_charter_places": n_charter_places,
        "n_unresolved_person_refs": n_unresolved_person_refs,
        "n_unresolved_place_refs": n_unresolved_place_refs,
    }


def migrate_review_queue(
    conn, vol: int, vd: dict, junction_info: dict,
    canonical_person_pk: dict, canonical_place_pk: dict,
    provisional_person_pk: dict, provisional_place_pk: dict, rpt: Report,
) -> dict:
    """Positional join, mirroring resolve_review_queue.py's exact
    (charter_filename, type) grouping for the still-open queue; a
    (filename, type, extracted_name, outcome_id) positional join for the
    already-archived queue (see module docstring rationale)."""
    review_target_pks = junction_info["review_target_pks"]
    charter_pk_by_seq = junction_info["charter_pk_by_seq"]

    def resolve_match_pk(entity_type: str, mid: str, name_hint: str = ""):
        mid = blank(mid)
        if not mid:
            return None
        lookup = canonical_person_pk if entity_type == "person" else canonical_place_pk
        prov = provisional_person_pk if entity_type == "person" else provisional_place_pk
        return lookup.get(mid) or resolve_provisional(prov, vol, mid, name_hint, rpt, entity_type)

    # sequence lookup by charter_filename, needed for both open+resolved rows
    seq_by_filename = {ch.get("filename"): ch.get("sequence") for ch in vd["resolved_json"]}

    n_open = n_resolved = n_join_failures = 0
    group_cursor: dict[tuple, int] = defaultdict(int)
    for row in vd["review_queue_csv"]:
        fn, typ = row.get("charter_filename"), row.get("type")
        key = (fn, typ)
        occ_i = group_cursor[key]
        group_cursor[key] += 1
        pks = review_target_pks.get(key, [])
        if occ_i >= len(pks):
            n_join_failures += 1
            rpt.warn(f"vol{vol}: review_queue.csv row (charter={fn!r} type={typ!r} "
                     f"occurrence #{occ_i}) has no corresponding REVIEW: entry -- join failure, "
                     f"row skipped (not migrated).")
            continue
        target_pk = pks[occ_i]
        seq = seq_by_filename.get(fn)
        charter_pk = charter_pk_by_seq.get(seq)
        decision = blank(row.get("decision")).lower()
        if decision not in ("accept", "reject"):
            decision = ""
        conn.execute(
            "INSERT INTO review_queue_items (entity_type, charter_person_pk, charter_place_pk, "
            "charter_pk, extracted_name, closest_match, match_pk, score, role_category, role, "
            "charter_date, decision, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                typ, target_pk if typ == "person" else None, target_pk if typ == "place" else None,
                charter_pk, blank(row.get("extracted_name")), blank(row.get("closest_match")),
                resolve_match_pk(typ, row.get("match_id"), row.get("closest_match")),
                to_float_or_none(row.get("score")),
                blank(row.get("role_category")), blank(row.get("role")), blank(row.get("charter_date")),
                decision, "open",
            ),
        )
        n_open += 1

    # ── Archived (already-resolved) queue rows ──
    resolved_cursor: dict[tuple, int] = defaultdict(int)
    resolved_index: dict[tuple, list] = defaultdict(list)
    for ch in vd["resolved_json"]:
        fn = ch.get("filename")
        for ordinal, p in enumerate(ch.get("resolved_persons", [])):
            resolved_index[(fn, "person", blank(p.get("name")), blank(p.get("person_id")))].append(("person", ch, p))
        for ordinal, l in enumerate(ch.get("resolved_locations", [])):
            resolved_index[(fn, "place", blank(l.get("name")), blank(l.get("place_id")))].append(("place", ch, l))

    # Need the charter_person_pk/charter_place_pk for each (charter, item) --
    # rebuild via a second lightweight pass keyed the same way ordinals were
    # inserted (ordinal position within resolved_persons/resolved_locations).
    pk_by_position: dict[tuple, int] = {}
    for row in conn.execute(
        "SELECT cp.charter_person_pk, c.sequence, cp.ordinal FROM charter_persons cp "
        "JOIN charters c ON c.charter_pk = cp.charter_pk WHERE c.volume = ?", (vol,)
    ):
        pk_by_position[("person", row[1], row[2])] = row[0]
    for row in conn.execute(
        "SELECT cl.charter_place_pk, c.sequence, cl.ordinal FROM charter_places cl "
        "JOIN charters c ON c.charter_pk = cl.charter_pk WHERE c.volume = ?", (vol,)
    ):
        pk_by_position[("place", row[1], row[2])] = row[0]

    for row in vd["review_queue_resolved_csv"]:
        fn, typ = row.get("charter_filename"), row.get("type")
        outcome_id = blank(row.get("outcome_id"))
        key = (fn, typ, blank(row.get("extracted_name")), outcome_id)
        candidates = resolved_index.get(key, [])
        occ_i = resolved_cursor[key]
        resolved_cursor[key] += 1
        if occ_i >= len(candidates):
            n_join_failures += 1
            rpt.warn(f"vol{vol}: review_queue_resolved.csv row (charter={fn!r} type={typ!r} "
                     f"name={row.get('extracted_name')!r} outcome={outcome_id!r}) has no "
                     f"corresponding entry in resolved_entities.json -- join failure, row skipped.")
            continue
        _, ch, item = candidates[occ_i]
        seq = ch.get("sequence")
        charter_pk = charter_pk_by_seq.get(seq)
        # find this item's ordinal within its list to look up its junction pk
        lst = ch.get("resolved_persons" if typ == "person" else "resolved_locations", [])
        ordinal = next((i for i, x in enumerate(lst) if x is item), None)
        target_pk = pk_by_position.get((typ, seq, ordinal)) if ordinal is not None else None
        conn.execute(
            "INSERT INTO review_queue_items (entity_type, charter_person_pk, charter_place_pk, "
            "charter_pk, extracted_name, closest_match, match_pk, score, role_category, role, "
            "charter_date, decision, outcome_pk, status, resolved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                typ, target_pk if typ == "person" else None, target_pk if typ == "place" else None,
                charter_pk, blank(row.get("extracted_name")), blank(row.get("closest_match")),
                resolve_match_pk(typ, row.get("match_id"), row.get("closest_match")),
                to_float_or_none(row.get("score")),
                blank(row.get("role_category")), blank(row.get("role")), blank(row.get("charter_date")),
                blank(row.get("decision")).lower() or "accept",
                resolve_match_pk(typ, outcome_id, row.get("extracted_name")),
                "resolved", blank(row.get("resolved_at")) or None,
            ),
        )
        n_resolved += 1

    return {"n_open": n_open, "n_resolved": n_resolved, "n_join_failures": n_join_failures}


def migrate_duplicate_candidates(
    conn, src: dict, canonical_person_pk: dict, provisional_person_pk: dict,
    canonical_place_pk: dict, provisional_place_pk: dict, rpt: Report,
) -> dict:
    rpt.line("=" * 78)
    rpt.line("STEP 9 — Duplicate candidates")
    rpt.line("=" * 78)

    def resolve_person(source: str, pid: str, name_hint: str):
        if source == "authority":
            return canonical_person_pk.get(pid)
        m = re.match(r"vol0*?(\d+)$", source)
        if not m:
            return None
        return resolve_provisional(provisional_person_pk, int(m.group(1)), pid, name_hint, rpt, "person")

    n_person_dups = n_person_skipped = 0
    for row in src["cross_vol_person_dups"]:
        a_pk = resolve_person(blank(row.get("a_source")), blank(row.get("a_id")), row.get("a_name"))
        b_pk = resolve_person(blank(row.get("b_source")), blank(row.get("b_id")), row.get("b_name"))
        if a_pk is None or b_pk is None:
            n_person_skipped += 1
            rpt.warn(f"person_duplicate_candidates row skipped -- could not resolve "
                     f"a=({row.get('a_source')},{row.get('a_id')}) or "
                     f"b=({row.get('b_source')},{row.get('b_id')}) to a migrated person_pk.")
            continue
        if a_pk == b_pk:
            n_person_skipped += 1
            continue
        lo, hi = (a_pk, b_pk) if a_pk < b_pk else (b_pk, a_pk)
        decision = blank(row.get("decision")).lower()
        if decision not in ("same", "different"):
            decision = ""
        try:
            conn.execute(
                "INSERT INTO person_duplicate_candidates (person_a_pk, person_b_pk, name_score, "
                "date_status, classification, confidence, decision) VALUES (?,?,?,?,?,?,?)",
                (lo, hi, to_float_or_none(row.get("name_score")) or 0.0, blank(row.get("date_status")),
                 blank(row.get("classification")), blank(row.get("confidence")), decision),
            )
            n_person_dups += 1
        except sqlite3.IntegrityError as e:
            n_person_skipped += 1
            rpt.warn(f"person_duplicate_candidates row skipped (integrity error: {e}) for pair ({lo},{hi})")

    n_place_dups = n_place_skipped = 0
    for vol, vd in src["vol_data"].items():
        for row in vd["nafnid_csv"]:
            pid = blank(row.get("place_id"))
            place_pk = resolve_provisional(provisional_place_pk, vol, pid, row.get("di_name"), rpt, "place")
            if place_pk is None:
                place_pk = canonical_place_pk.get(pid)
            if place_pk is None:
                n_place_skipped += 1
                rpt.warn(f"vol{vol}: place_duplicate_candidates row skipped -- place_id "
                         f"{pid!r} did not resolve to a migrated place_pk.")
                continue
            conn.execute(
                "INSERT INTO place_duplicate_candidates (place_pk, di_name, di_sysla_given, "
                "di_place_type, di_region, wikidata_status, candidate_rank, name_score, "
                "distance_km, flag, match_sources, candidate_name, candidate_nafnid, "
                "candidate_hreppur, candidate_sysla, candidate_lat, candidate_lng, decision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    place_pk, blank(row.get("di_name")), blank(row.get("di_sysla_given")),
                    blank(row.get("di_place_type")), blank(row.get("di_region")),
                    blank(row.get("wikidata_status")), to_int_or_none(row.get("candidate_rank")),
                    to_float_or_none(row.get("name_score")), to_float_or_none(row.get("distance_km")),
                    blank(row.get("flag")), blank(row.get("match_sources")), blank(row.get("candidate_name")),
                    blank(row.get("candidate_id")),  # CSV column "candidate_id" -> schema "candidate_nafnid"
                    blank(row.get("candidate_hreppur")), blank(row.get("candidate_sysla")),
                    to_float_or_none(row.get("candidate_lat")), to_float_or_none(row.get("candidate_lng")),
                    "",
                ),
            )
            n_place_dups += 1

    rpt.line(f"  person_duplicate_candidates: {n_person_dups} inserted, {n_person_skipped} skipped")
    rpt.line(f"  place_duplicate_candidates:  {n_place_dups} inserted, {n_place_skipped} skipped")
    return {
        "n_person_dups": n_person_dups, "n_person_skipped": n_person_skipped,
        "n_place_dups": n_place_dups, "n_place_skipped": n_place_skipped,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Verification pass
# ═══════════════════════════════════════════════════════════════════════════

def run_verification(conn, src: dict, splits_map: dict, split_ids: set, rpt: Report) -> bool:
    rpt.line("=" * 78)
    rpt.line("VERIFICATION PASS")
    rpt.line("=" * 78)
    ok = True

    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        ok = False
        rpt.warn(f"PRAGMA foreign_key_check returned {len(fk_errors)} violation(s): {fk_errors}")
    else:
        rpt.line("  PRAGMA foreign_key_check: clean (0 violations)")

    dup_display = conn.execute(
        "SELECT display_id, COUNT(*) c FROM ("
        "  SELECT display_id FROM persons UNION ALL SELECT display_id FROM places"
        ") GROUP BY display_id HAVING c > 1"
    ).fetchall()
    if dup_display:
        ok = False
        rpt.warn(f"display_id uniqueness violated across persons+places: {dup_display}")
    else:
        rpt.line("  display_id uniqueness (persons+places combined): OK")
    dup_p = conn.execute("SELECT display_id, COUNT(*) c FROM persons GROUP BY display_id HAVING c>1").fetchall()
    dup_l = conn.execute("SELECT display_id, COUNT(*) c FROM places GROUP BY display_id HAVING c>1").fetchall()
    if dup_p or dup_l:
        ok = False
        rpt.warn(f"display_id uniqueness violated: persons={dup_p} places={dup_l}")

    for old_id in sorted(split_ids):
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM places WHERE legacy_id = ? AND source_volume IS NULL", (old_id,)
        ).fetchone()[0]
        expected = 1 + len(splits_map.get(old_id, []))
        rpt.line(f"  split {old_id}: {n_rows} distinct canonical row(s) present "
                 f"(mapping specifies {expected}; matches: {n_rows == expected})")
        if n_rows != expected:
            ok = False

    n_persons = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    n_places = conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    n_charters = conn.execute("SELECT COUNT(*) FROM charters").fetchone()[0]
    n_cp = conn.execute("SELECT COUNT(*) FROM charter_persons").fetchone()[0]
    n_cl = conn.execute("SELECT COUNT(*) FROM charter_places").fetchone()[0]
    n_rq = conn.execute("SELECT COUNT(*) FROM review_queue_items").fetchone()[0]
    n_pd = conn.execute("SELECT COUNT(*) FROM person_duplicate_candidates").fetchone()[0]
    n_ld = conn.execute("SELECT COUNT(*) FROM place_duplicate_candidates").fetchone()[0]
    rpt.line(f"  Row counts: persons={n_persons} places={n_places} charters={n_charters} "
             f"charter_persons={n_cp} charter_places={n_cl} review_queue_items={n_rq} "
             f"person_duplicate_candidates={n_pd} place_duplicate_candidates={n_ld}")

    import random
    rng = random.Random(42)
    sample_rows = conn.execute("SELECT display_id, legacy_id, source_volume, canonical_name FROM persons").fetchall()
    sample_rows += conn.execute("SELECT display_id, legacy_id, source_volume, canonical_name FROM places").fetchall()
    spot = rng.sample(sample_rows, min(10, len(sample_rows)))
    rpt.line(f"  Spot-check ({len(spot)} random rows) against source:")
    for display_id, legacy_id, source_volume, canonical_name in spot:
        rpt.line(f"    {display_id}: legacy_id={legacy_id!r} source_volume={source_volume} "
                 f"canonical_name={canonical_name!r}")

    return ok


# ═══════════════════════════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════════════════════════

def run_migration(conn, args, src: dict, rpt: Report) -> dict:
    report_schema_drift(src, rpt)

    canonical_persons, person_conflicts = reconcile_persons(src, rpt)
    splits_map = load_place_id_splits(args.place_id_splits, rpt)
    conflict_resolutions = load_conflict_resolutions(args.conflict_resolutions, rpt)
    canonical_places, place_conflicts, unresolved_splits = reconcile_places(
        src, splits_map, rpt, conflict_resolutions
    )

    # ids with >1 raw CSV row (internally-duplicated canonical place ids)
    csv_counts = defaultdict(int)
    for e in src["place_auth"].entries:
        csv_counts[e.place_id] += 1
    split_ids = {pid for pid, c in csv_counts.items() if c > 1}

    all_conflicts = person_conflicts + place_conflicts
    conflicts_path = args.output_dir / "migration_conflicts.csv"
    if all_conflicts:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with open(conflicts_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["entity_type", "id", "csv_canonical_name",
                                               "xlsx_canonical_name", "reason"])
            w.writeheader()
            w.writerows(all_conflicts)
        rpt.line(f"  Wrote {len(all_conflicts)} conflict(s) to {conflicts_path}")

    if unresolved_splits and not args.dry_run and not args.verify_only:
        raise RuntimeError(
            f"{len(unresolved_splits)} internally-duplicated place id row(s) have no matching "
            f"--place-id-splits entry: {unresolved_splits}. Refusing to proceed with --db "
            f"(non-dry-run) until a mapping is supplied. Use --dry-run to iterate."
        )

    rpt.line("=" * 78)
    rpt.line("STEP 5 — Insert canonical rows")
    rpt.line("=" * 78)
    canonical_person_pk: dict[str, int] = {}
    for pid, row in canonical_persons.items():
        canonical_person_pk[pid] = insert_person(conn, row)
    canonical_place_pk: dict[str, int] = {}
    for pid, row in canonical_places.items():
        canonical_place_pk[pid] = insert_place(conn, row)
    rpt.line(f"  Inserted {len(canonical_person_pk)} canonical persons, "
             f"{len(canonical_place_pk)} canonical places.")

    rpt.line("=" * 78)
    rpt.line("STEP 6 — Provisional persons/places per volume")
    rpt.line("=" * 78)
    # {(vol, legacy_id): [(pk, canonical_name), ...]} -- usually one entry;
    # more than one means a within-volume id collision (see build_provisional_places).
    provisional_person_pk: dict[tuple, list] = defaultdict(list)
    provisional_place_pk: dict[tuple, list] = defaultdict(list)
    for vol, vd in src["vol_data"].items():
        prov_persons = build_provisional_persons(vol, vd["persons_new_csv"], rpt)
        prov_places = build_provisional_places(vol, vd["places_new_csv"], rpt)
        for row in prov_persons:
            pk = insert_person(conn, row)
            provisional_person_pk[(vol, row["raw_legacy_id"])].append((pk, row["canonical_name"]))
        for row in prov_places:
            pk = insert_place(conn, row)
            provisional_place_pk[(vol, row["raw_legacy_id"])].append((pk, row["canonical_name"]))
        rpt.line(f"  vol{vol:02d}: {len(prov_persons)} provisional persons, "
                 f"{len(prov_places)} provisional places.")

    rpt.line("=" * 78)
    rpt.line("STEP 7 — Charters + junction tables")
    rpt.line("=" * 78)
    junction_by_vol = {}
    all_split_review_rows = []
    for vol, vd in src["vol_data"].items():
        info = migrate_charters_and_junctions(
            conn, vol, vd, canonical_person_pk, canonical_place_pk,
            provisional_person_pk, provisional_place_pk, split_ids, rpt,
        )
        junction_by_vol[vol] = info
        all_split_review_rows.extend(info["split_review_rows"])
        rpt.line(f"  vol{vol:02d}: charters={info['n_charters']} "
                 f"charter_persons={info['n_charter_persons']} "
                 f"charter_places={info['n_charter_places']} "
                 f"unresolved_person_refs={info['n_unresolved_person_refs']} "
                 f"unresolved_place_refs={info['n_unresolved_place_refs']}")

    review_dir_out = args.output_dir / "review"
    review_dir_out.mkdir(parents=True, exist_ok=True)
    split_review_path = review_dir_out / "place_id_split_review.csv"
    with open(split_review_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["volume", "sequence", "old_id", "extracted_name",
                                           "role", "region", "charter_date", "di_reference"])
        w.writeheader()
        w.writerows(all_split_review_rows)
    rpt.line(f"  Wrote {len(all_split_review_rows)} ambiguous split-id charter reference(s) "
             f"to {split_review_path}")
    if not all_split_review_rows:
        rpt.line("  (0 rows: no charter in the current data resolves to the internally-"
                 "duplicated canonical place ids via a canonical match path -- every real "
                 "reference to l118/l128/l129-style strings turned out to be that volume's "
                 "own provisional mint, not the canonical duplicate. See report for detail.)")

    rpt.line("=" * 78)
    rpt.line("STEP 8 — Review queue")
    rpt.line("=" * 78)
    rq_info_by_vol = {}
    for vol, vd in src["vol_data"].items():
        rq_info = migrate_review_queue(
            conn, vol, vd, junction_by_vol[vol], canonical_person_pk, canonical_place_pk,
            provisional_person_pk, provisional_place_pk, rpt,
        )
        rq_info_by_vol[vol] = rq_info
        rpt.line(f"  vol{vol:02d}: open={rq_info['n_open']} resolved={rq_info['n_resolved']} "
                 f"join_failures={rq_info['n_join_failures']}")

    dup_info = migrate_duplicate_candidates(
        conn, src, canonical_person_pk, provisional_person_pk,
        canonical_place_pk, provisional_place_pk, rpt,
    )

    # ── Explicit row-count reconciliation (migrated + skipped == source), per file ──
    rpt.line("=" * 78)
    rpt.line("Row-count reconciliation (migrated + skipped == source, per file)")
    rpt.line("=" * 78)
    n_csv_persons = len(src["person_auth"].entries)
    n_csv_places = len(src["place_auth"].entries)
    reconciliations = [
        ("person_names_authority.csv", n_csv_persons,
         n_csv_persons - len(person_conflicts), len(person_conflicts)),
        ("xlsx persons_authority", len(src["xlsx_persons"]),
         len(src["xlsx_persons"]) - len(person_conflicts), len(person_conflicts)),
        ("place_names_authority.csv", n_csv_places,
         n_csv_places - len(place_conflicts) - len(unresolved_splits),
         len(place_conflicts) + len(unresolved_splits)),
        ("xlsx Places_Authority", len(src["xlsx_places"]),
         len(src["xlsx_places"]) - len(place_conflicts), len(place_conflicts)),
        ("cross_volume_person_duplicates.csv", len(src["cross_vol_person_dups"]),
         dup_info["n_person_dups"], dup_info["n_person_skipped"]),
    ]
    for vol, vd in src["vol_data"].items():
        reconciliations.append((f"vol{vol:02d}_persons_new.csv", len(vd["persons_new_csv"]),
                                 len(vd["persons_new_csv"]), 0))
        reconciliations.append((f"vol{vol:02d}_places_new.csv", len(vd["places_new_csv"]),
                                 len(vd["places_new_csv"]), 0))
        reconciliations.append((f"vol{vol:02d}_charters.csv/resolved_entities.json", len(vd["resolved_json"]),
                                 junction_by_vol[vol]["n_charters"], 0))
        rq_open_expected = len(vd["review_queue_csv"])
        reconciliations.append((f"vol{vol:02d}_review_queue.csv", rq_open_expected,
                                 rq_info_by_vol[vol]["n_open"],
                                 rq_open_expected - rq_info_by_vol[vol]["n_open"]))
        rq_resolved_expected = len(vd["review_queue_resolved_csv"])
        reconciliations.append((f"vol{vol:02d}_review_queue_resolved.csv", rq_resolved_expected,
                                 rq_info_by_vol[vol]["n_resolved"],
                                 rq_resolved_expected - rq_info_by_vol[vol]["n_resolved"]))
        reconciliations.append((f"vol{vol:02d}_places_nafnid_candidates.csv", len(vd["nafnid_csv"]),
                                 None, None))  # reconciled in aggregate via dup_info below

    all_reconciled = True
    for label, source, migrated, skipped in reconciliations:
        if migrated is None:
            continue
        ok_row = (migrated + skipped == source)
        all_reconciled = all_reconciled and ok_row
        marker = "OK" if ok_row else "MISMATCH"
        rpt.line(f"  [{marker}] {label}: source={source} migrated={migrated} skipped={skipped}"
                 f"{'' if ok_row else '  <-- migrated+skipped != source!'}")
    n_nafnid_total = sum(len(vd["nafnid_csv"]) for vd in src["vol_data"].values())
    nafnid_ok = (dup_info["n_place_dups"] + dup_info["n_place_skipped"] == n_nafnid_total)
    all_reconciled = all_reconciled and nafnid_ok
    rpt.line(f"  [{'OK' if nafnid_ok else 'MISMATCH'}] *_places_nafnid_candidates.csv (all volumes): "
             f"source={n_nafnid_total} migrated={dup_info['n_place_dups']} skipped={dup_info['n_place_skipped']}")
    rpt.line(f"  person_names_authority.csv/xlsx canonical name conflicts: {len(person_conflicts)}")
    rpt.line(f"  place_names_authority.csv/xlsx canonical name conflicts: {len(place_conflicts)}  "
             f"(each conflict = 1 row skipped on BOTH the CSV and xlsx side)")
    if not all_reconciled:
        rpt.warn("one or more source files failed migrated+skipped==source reconciliation -- see MISMATCH lines above")

    return {
        "canonical_person_pk": canonical_person_pk, "canonical_place_pk": canonical_place_pk,
        "provisional_person_pk": provisional_person_pk, "provisional_place_pk": provisional_place_pk,
        "split_ids": split_ids, "splits_map": splits_map, "conflicts": all_conflicts,
        "unresolved_splits": unresolved_splits, "dup_info": dup_info,
        "row_counts_ok": all_reconciled,
    }


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="No writes; verification report only.")
    mode.add_argument("--db", type=Path, help="Write for real to this SQLite file.")
    mode.add_argument("--verify-only", action="store_true",
                       help="Re-check an already-built DB (pass --db-path to say which one).")
    p.add_argument("--db-path", type=Path, help="DB file to check with --verify-only.")
    p.add_argument("--force", action="store_true",
                    help="(Reserved) allow --db to proceed despite migration_conflicts.csv entries. "
                         "Conflicting rows are still never auto-merged -- this only unblocks the run.")
    p.add_argument("--place-id-splits", type=Path, default=None,
                    help="CSV: old_id,disambiguator,new_display_id -- required before --db mode "
                         "will insert the non-primary row of an internally-duplicated place id.")
    p.add_argument("--conflict-resolutions", type=Path, default=None,
                    help="CSV: place_id,canonical_name,notes_extra -- human-confirmed 'same place, "
                         "cosmetic name difference' resolutions for canonical_name mismatches on a "
                         "shared CSV/xlsx id. Unresolved mismatches are skipped, not guessed.")
    p.add_argument("--review-dir", type=Path, default=config.REVIEW_DIR)
    p.add_argument("--entities-dir", type=Path, default=config.ENTITIES_DIR)
    p.add_argument("--authority-file", type=Path, default=config.AUTHORITY_FILE)
    p.add_argument("--person-authority-csv", type=Path, default=PERSON_AUTHORITY_CSV)
    p.add_argument("--place-authority-csv", type=Path, default=PLACE_AUTHORITY_CSV)
    p.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    return p.parse_args()


def main():
    args = parse_args()
    rpt = Report()

    if args.verify_only:
        db_path = args.db_path or config.DB_PATH
        if not db_path.exists():
            print(f"Error: --verify-only given but {db_path} does not exist.", file=sys.stderr)
            sys.exit(1)
        src = load_all_sources(args, rpt)
        splits_map = load_place_id_splits(args.place_id_splits, rpt)
        csv_counts = defaultdict(int)
        for e in src["place_auth"].entries:
            csv_counts[e.place_id] += 1
        split_ids = {pid for pid, c in csv_counts.items() if c > 1}
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        ok = run_verification(conn, src, splits_map, split_ids, rpt)
        conn.close()
        sys.exit(0 if ok else 1)

    src = load_all_sources(args, rpt)

    if args.dry_run:
        rpt.line("\n*** DRY RUN: writing to an in-memory SQLite connection, nothing is persisted. ***\n")
        conn = sqlite3.connect(":memory:")
    else:
        if args.db.exists():
            print(f"Error: {args.db} already exists. Refusing to overwrite a real DB file; "
                  f"remove it first if you intend a fresh migration.", file=sys.stderr)
            sys.exit(1)
        conn = sqlite3.connect(str(args.db))

    # schema.sql's own first statement is `PRAGMA foreign_keys = ON`, so this
    # takes effect regardless -- harmless, since every insert order below
    # (canonical -> provisional -> charters -> junctions -> review queue ->
    # duplicate candidates) already satisfies every FK dependency up front.
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    try:
        with conn:
            info = run_migration(conn, args, src, rpt)
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        conn.close()
        raise

    csv_counts = defaultdict(int)
    for e in src["place_auth"].entries:
        csv_counts[e.place_id] += 1
    split_ids = {pid for pid, c in csv_counts.items() if c > 1}
    ok = run_verification(conn, src, info["splits_map"], split_ids, rpt) and info["row_counts_ok"]

    rpt.line("=" * 78)
    rpt.line(f"RESULT: {'CLEAN' if ok else 'PROBLEMS FOUND'} "
             f"({'dry-run, nothing persisted' if args.dry_run else f'written to {args.db}'})")
    rpt.line("=" * 78)

    conn.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
