"""
Canonical data-access layer for the DI charter pipeline, backed by
charter_pipeline.db (SQLite). Replaces per-volume CSVs and the two
independently-mutable authority stores (person_names_authority.csv /
place_names_authority.csv vs. the master xlsx's persons_authority /
Places_Authority sheets) that had already diverged in practice -- see
schema.sql's header comment and migrate_to_sqlite.py for the one-time
population from those old sources.

This module is imported directly (not digit-prefixed), same convention as
config.py/person_authority.py/place_authority.py.
"""
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SNAPSHOT_DIR = Path(__file__).parent / ".snapshots"
UNDO_LOG_PATH = SNAPSHOT_DIR / "undo_log.json"
UNDO_STACK_SIZE = 20

# Mirrors person_authority.py's/place_authority.py's identical _PAREN_TAIL +
# split_variants() (previously duplicated between those two modules) -- one
# shared copy here now that everything can import db.py.
_PAREN_TAIL = re.compile(r'\s*\([^)]*\)\s*$')

_CHARTER_YEAR_RE = re.compile(r"(\d{3,4})")


def charter_year(date_str) -> str:
    """Mirrors 03_resolve_entities.py's/resolve_review_queue.py's charter_year()."""
    m = _CHARTER_YEAR_RE.match((date_str or "").strip())
    if not m:
        return ""
    return str(int(m.group(1)))


def split_variants(raw: str) -> list[str]:
    """Split on semicolons that are NOT inside parentheses."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in (raw or ""):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif ch == ';' and depth == 0:
            part = ''.join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    part = ''.join(buf).strip()
    if part:
        parts.append(part)
    return parts


def _all_names(canonical_name: str, variant_names: str) -> list[str]:
    """All known name forms, lowercased, with parenthetical-stripped
    duplicates -- mirrors PersonEntry.all_names()/PlaceEntry.all_names()."""
    def _add(names: list[str], s: str) -> None:
        s = (s or "").strip().strip('"').strip("'").lower()
        if s:
            names.append(s)
            base = _PAREN_TAIL.sub('', s).strip()
            if base and base != s:
                names.append(base)

    names: list[str] = []
    _add(names, canonical_name)
    for v in split_variants(variant_names):
        _add(names, v)
    return list(dict.fromkeys(names))


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Applies schema.sql to a fresh (or existing, idempotent-if-empty) db file."""
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backup + undo (schema.sql section 1.6/1.7 of the migration plan)
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.strip().lower())[:40].strip("_")


def _timestamp() -> str:
    # Deliberately NOT datetime.now()/time.time() at import/module-load time --
    # this is called at actual mutation time from live code, not from a
    # workflow script context, so wall-clock time is fine here.
    return time.strftime("%Y%m%dT%H%M%S")


def _read_undo_log() -> list[dict]:
    import json
    if not UNDO_LOG_PATH.exists():
        return []
    return json.loads(UNDO_LOG_PATH.read_text(encoding="utf-8"))


def _write_undo_log(entries: list[dict]) -> None:
    import json
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    UNDO_LOG_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def with_undo(description: str, fn, *args, **kwargs):
    """Snapshots DB_PATH before running fn(*args, **kwargs), records the
    snapshot + description in the undo journal on success, trims the journal
    to the last UNDO_STACK_SIZE entries. fn should perform its own commit/
    rollback (typically via a `with conn:` block)."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap_name = f"{_timestamp()}__{_slugify(description)}.db"
    snap_path = SNAPSHOT_DIR / snap_name
    if Path(DB_PATH).exists():
        shutil.copy2(DB_PATH, snap_path)

    result = fn(*args, **kwargs)

    entries = _read_undo_log()
    entries.append({
        "timestamp": _timestamp(),
        "description": description,
        "snapshot_path": str(snap_path),
    })
    entries = entries[-UNDO_STACK_SIZE:]
    _write_undo_log(entries)

    # Prune snapshot files no longer referenced by the trimmed journal.
    kept = {Path(e["snapshot_path"]).name for e in entries}
    for f in SNAPSHOT_DIR.glob("*.db"):
        if f.name not in kept:
            f.unlink(missing_ok=True)

    return result


def get_last_action() -> dict | None:
    entries = _read_undo_log()
    return entries[-1] if entries else None


def undo_last_action() -> dict:
    entries = _read_undo_log()
    if not entries:
        raise RuntimeError("Nothing to undo.")
    last = entries.pop()
    snap_path = Path(last["snapshot_path"])
    if not snap_path.exists():
        raise RuntimeError(f"Snapshot missing on disk: {snap_path}")
    shutil.copy2(snap_path, DB_PATH)
    snap_path.unlink(missing_ok=True)
    _write_undo_log(entries)
    return {"description": last["description"], "restored_at": _timestamp()}


# ═══════════════════════════════════════════════════════════════════════════
# Persons
# ═══════════════════════════════════════════════════════════════════════════

def get_persons(status: str | None = None, source_volume: int | None = None,
                 review_status: str | None = None,
                 conn: sqlite3.Connection | None = None) -> pd.DataFrame:
    own = conn is None
    conn = conn or get_connection()
    try:
        q, params = "SELECT * FROM persons WHERE 1=1", []
        if status is not None:
            q += " AND status = ?"; params.append(status)
        if source_volume is not None:
            q += " AND source_volume = ?"; params.append(source_volume)
        if review_status is not None:
            q += " AND review_status = ?"; params.append(review_status)
        # ORDER BY is required, not cosmetic: without it SQLite doesn't
        # guarantee the same row order across two calls returning identical
        # rows, which broke UI dirty-checking (row-order-sensitive .equals())
        # and made data_editor-derived panels flicker in and out on reruns.
        q += " ORDER BY person_pk"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        if own:
            conn.close()


def get_person_by_pk(person_pk: int, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute("SELECT * FROM persons WHERE person_pk = ?", (person_pk,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def lookup_person_by_name(name: str, status: str = "canonical",
                            conn: sqlite3.Connection | None = None) -> dict | None:
    """Exact case-insensitive lookup by any known name form -- mirrors
    PersonAuthority.lookup(). Builds the name index fresh each call; at this
    project's data scale (hundreds of rows) that's trivial, and it avoids
    ever serving a stale in-memory index after a concurrent mutation."""
    own = conn is None
    conn = conn or get_connection()
    try:
        target = (name or "").strip().lower()
        if not target:
            return None
        for row in conn.execute("SELECT * FROM persons WHERE status = ?", (status,)):
            if target in _all_names(row["canonical_name"], row["variant_names"]):
                return dict(row)
        return None
    finally:
        if own:
            conn.close()


def lookup_person_by_wikidata(qid: str, status: str = "canonical",
                                conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_connection()
    try:
        qid = (qid or "").strip()
        if not qid:
            return None
        row = conn.execute(
            "SELECT * FROM persons WHERE status = ? AND wikidata_id = ? LIMIT 1", (status, qid)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def find_person(canonical_name: str, wikidata_id: str = "", variant_names: list[str] | None = None,
                  status: str = "canonical", conn: sqlite3.Connection | None = None) -> dict | None:
    """Multi-strategy lookup -- mirrors PersonAuthority.find() exactly:
    1. canonical_name exact match, 1b. with trailing parenthetical stripped,
    2. wikidata_id match, 3. any variant_name exact match."""
    own = conn is None
    conn = conn or get_connection()
    try:
        entry = lookup_person_by_name(canonical_name, status, conn)
        if entry:
            return entry
        stripped = _PAREN_TAIL.sub("", canonical_name).strip()
        if stripped and stripped != canonical_name:
            entry = lookup_person_by_name(stripped, status, conn)
            if entry:
                return entry
        if wikidata_id:
            entry = lookup_person_by_wikidata(wikidata_id, status, conn)
            if entry:
                return entry
        for v in (variant_names or []):
            entry = lookup_person_by_name(v, status, conn)
            if entry:
                return entry
        return None
    finally:
        if own:
            conn.close()


def insert_provisional_person(source_volume: int, legacy_id: str, canonical_name: str,
                                **fields) -> int:
    """Mints display_id = 'v{NN}-{legacy_id}' (collision-free by construction
    across volumes -- replaces 03_resolve_entities.py's next_id())."""
    display_id = f"v{source_volume:02d}-{legacy_id}"
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO persons
                   (display_id, legacy_id, source_volume, status, canonical_name,
                    variant_names, wikidata_id, patronymic, occupation, title,
                    floruit_start, floruit_end, gender, associated_places, notes, sources)
                   VALUES (?, ?, ?, 'provisional', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (display_id, legacy_id, source_volume, canonical_name,
                 fields.get("variant_names", ""), fields.get("wikidata_id", ""),
                 fields.get("patronymic", ""), fields.get("occupation", ""),
                 fields.get("title", ""), fields.get("floruit_start"), fields.get("floruit_end"),
                 fields.get("gender", ""), fields.get("associated_places", ""),
                 fields.get("notes", ""), fields.get("sources", "")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def update_person(person_pk: int, **fields) -> None:
    """Updates whichever columns are passed (e.g. review_status, canonical_name,
    notes edited via New Entities' data_editor)."""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                f"UPDATE persons SET {cols}, updated_at = datetime('now') WHERE person_pk = ?",
                (*fields.values(), person_pk),
            )
    finally:
        conn.close()


def _promote_persons_batch_impl(person_pks: list[int]) -> dict:
    conn = get_connection()
    added, skipped_existing = [], []
    try:
        with conn:
            for pk in person_pks:
                row = conn.execute(
                    "SELECT status FROM persons WHERE person_pk = ?", (pk,)
                ).fetchone()
                if row is None:
                    continue
                if row["status"] == "canonical":
                    skipped_existing.append(pk)
                    continue
                conn.execute(
                    "UPDATE persons SET status='canonical', updated_at=datetime('now') "
                    "WHERE person_pk = ?", (pk,),
                )
                added.append(pk)
    finally:
        conn.close()
    return {"added": added, "skipped_existing": skipped_existing}


def promote_persons_batch(person_pks: list[int]) -> dict:
    return with_undo(f"Promoted {len(person_pks)} person(s) to authority",
                      _promote_persons_batch_impl, person_pks)


PERSON_UNION_FIELDS = ["variant_names", "notes", "sources", "associated_places"]
PERSON_FIRST_NONBLANK_FIELDS = ["patronymic", "occupation", "title", "gender", "wikidata_id"]


def _union_semicolon(values: list[str]) -> str:
    seen, out = set(), []
    for v in values:
        for item in (v or "").split(";"):
            item = item.strip()
            key = item.lower()
            if item and key not in seen:
                seen.add(key)
                out.append(item)
    return ";".join(out)


def _first_nonblank(values: list[str]) -> str:
    for v in values:
        if (v or "").strip():
            return v
    return ""


def _merged_volumes_union(rows: list[dict]) -> str:
    """rows[0] is the survivor. source_volume is never overwritten by a
    merge -- combining two records that originated in different volumes can
    legitimately mean the same person/place is genuinely attested in
    charters across both volumes, so silently picking one would be actively
    dangerous, not just imprecise. Every OTHER row's source_volume, plus
    whatever any row (including the survivor, from an earlier merge) already
    carries in merged_volumes, gets folded in here as the visible trail;
    the survivor's own source_volume is excluded since that one is already
    visible via source_volume itself."""
    survivor_volume = rows[0].get("source_volume")
    values = [r.get("merged_volumes") or "" for r in rows]
    values += [str(r["source_volume"]) for r in rows[1:] if r.get("source_volume") is not None]
    combined = _union_semicolon(values)
    if survivor_volume is not None:
        combined = ";".join(v for v in combined.split(";") if v.strip() != str(survivor_volume))
    return combined


def _merge_persons_impl(survivor_pk: int, dropped_pks: list[int]) -> dict:
    conn = get_connection()
    try:
        with conn:
            rows = [dict(conn.execute(
                "SELECT * FROM persons WHERE person_pk = ?", (pk,)
            ).fetchone()) for pk in [survivor_pk, *dropped_pks]]
            survivor_row = rows[0]
            merged = dict(survivor_row)

            for field in PERSON_UNION_FIELDS:
                vals = [r.get(field, "") for r in rows]
                if field == "variant_names":
                    vals = vals + [r.get("canonical_name", "") for r in rows[1:]]
                merged[field] = _union_semicolon(vals)
            survivor_name_key = merged["canonical_name"].strip().lower()
            merged["variant_names"] = ";".join(
                v for v in merged["variant_names"].split(";")
                if v.strip().lower() != survivor_name_key
            )
            for field in PERSON_FIRST_NONBLANK_FIELDS:
                merged[field] = _first_nonblank([r.get(field, "") for r in rows])
            starts = [r.get("floruit_start") for r in rows if r.get("floruit_start") is not None]
            ends = [r.get("floruit_end") for r in rows if r.get("floruit_end") is not None]
            merged["floruit_start"] = min(starts) if starts else None
            merged["floruit_end"] = max(ends) if ends else None
            merged["merged_volumes"] = _merged_volumes_union(rows)

            conn.execute(
                """UPDATE persons SET variant_names=?, notes=?, sources=?, associated_places=?,
                   patronymic=?, occupation=?, title=?, gender=?, wikidata_id=?,
                   floruit_start=?, floruit_end=?, merged_volumes=?, updated_at=datetime('now')
                   WHERE person_pk = ?""",
                (merged["variant_names"], merged["notes"], merged["sources"],
                 merged["associated_places"], merged["patronymic"], merged["occupation"],
                 merged["title"], merged["gender"], merged["wikidata_id"],
                 merged["floruit_start"], merged["floruit_end"], merged["merged_volumes"], survivor_pk),
            )
            # Relink every reference (junction rows, review-queue outcomes,
            # duplicate candidates) from the dropped ids to the survivor --
            # this is the operation merge_entities.py's file-based version
            # could only do within one volume; here it's global.
            for pk in dropped_pks:
                conn.execute("UPDATE charter_persons SET person_pk=? WHERE person_pk=?", (survivor_pk, pk))
                conn.execute("UPDATE charter_persons SET review_match_person_pk=? WHERE review_match_person_pk=?",
                             (survivor_pk, pk))
                conn.execute("UPDATE review_queue_items SET match_pk=? WHERE match_pk=? AND entity_type='person'",
                             (survivor_pk, pk))
                conn.execute("UPDATE review_queue_items SET outcome_pk=? WHERE outcome_pk=? AND entity_type='person'",
                             (survivor_pk, pk))
                conn.execute(
                    "UPDATE person_duplicate_candidates SET person_a_pk=? WHERE person_a_pk=?",
                    (survivor_pk, pk),
                )
                conn.execute(
                    "UPDATE person_duplicate_candidates SET person_b_pk=? WHERE person_b_pk=?",
                    (survivor_pk, pk),
                )
                conn.execute("DELETE FROM persons WHERE person_pk = ?", (pk,))
    finally:
        conn.close()
    return {"survivor_pk": survivor_pk, "dropped_pks": dropped_pks}


def merge_persons(survivor_pk: int, dropped_pks: list[int]) -> dict:
    """Merges 2+ person rows into one (any status/volume combination --
    unlike the old merge_entities.py, this is not restricted to one volume
    since ids are global now). survivor_pk should be the lowest person_pk
    (== first-minted, same 'oldest wins' rule as pick_survivor() used)."""
    if len(dropped_pks) < 1:
        raise ValueError("Need at least one id to merge into the survivor.")
    return with_undo(f"Merged {len(dropped_pks)} person(s) into person_pk={survivor_pk}",
                      _merge_persons_impl, survivor_pk, dropped_pks)


# ═══════════════════════════════════════════════════════════════════════════
# Places
# ═══════════════════════════════════════════════════════════════════════════

def get_places(status: str | None = None, source_volume: int | None = None,
                review_status: str | None = None,
                conn: sqlite3.Connection | None = None) -> pd.DataFrame:
    own = conn is None
    conn = conn or get_connection()
    try:
        q, params = "SELECT * FROM places WHERE 1=1", []
        if status is not None:
            q += " AND status = ?"; params.append(status)
        if source_volume is not None:
            q += " AND source_volume = ?"; params.append(source_volume)
        if review_status is not None:
            q += " AND review_status = ?"; params.append(review_status)
        # See get_persons()'s comment -- ORDER BY is required for stable
        # row order across calls, not cosmetic.
        q += " ORDER BY place_pk"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        if own:
            conn.close()


def get_place_by_pk(place_pk: int, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute("SELECT * FROM places WHERE place_pk = ?", (place_pk,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def lookup_place_by_name(name: str, status: str = "canonical",
                           conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_connection()
    try:
        target = (name or "").strip().lower()
        if not target:
            return None
        for row in conn.execute("SELECT * FROM places WHERE status = ?", (status,)):
            if target in _all_names(row["canonical_name"], row["variant_names"]):
                return dict(row)
        return None
    finally:
        if own:
            conn.close()


def lookup_place_by_wikidata(qid: str, status: str = "canonical",
                               conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_connection()
    try:
        qid = (qid or "").strip()
        if not qid:
            return None
        row = conn.execute(
            "SELECT * FROM places WHERE status = ? AND wikidata_id = ? LIMIT 1", (status, qid)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def find_place(canonical_name: str, wikidata_id: str = "", variant_names: list[str] | None = None,
                status: str = "canonical", conn: sqlite3.Connection | None = None) -> dict | None:
    """Mirrors PlaceAuthority.find() exactly (see find_person for the same shape)."""
    own = conn is None
    conn = conn or get_connection()
    try:
        entry = lookup_place_by_name(canonical_name, status, conn)
        if entry:
            return entry
        stripped = _PAREN_TAIL.sub("", canonical_name).strip()
        if stripped and stripped != canonical_name:
            entry = lookup_place_by_name(stripped, status, conn)
            if entry:
                return entry
        if wikidata_id:
            entry = lookup_place_by_wikidata(wikidata_id, status, conn)
            if entry:
                return entry
        for v in (variant_names or []):
            entry = lookup_place_by_name(v, status, conn)
            if entry:
                return entry
        return None
    finally:
        if own:
            conn.close()


def insert_provisional_place(source_volume: int, legacy_id: str, canonical_name: str,
                               **fields) -> int:
    display_id = f"v{source_volume:02d}-{legacy_id}"
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO places
                   (display_id, legacy_id, source_volume, status, canonical_name, variant_names,
                    place_type, coordinates_lat, coordinates_long, region, district,
                    modern_equivalent, wikidata_id, nafnid_id, geo_match_score, notes, sources)
                   VALUES (?, ?, ?, 'provisional', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (display_id, legacy_id, source_volume, canonical_name,
                 fields.get("variant_names", ""), fields.get("place_type", ""),
                 fields.get("coordinates_lat"), fields.get("coordinates_long"),
                 fields.get("region", ""), fields.get("district", ""),
                 fields.get("modern_equivalent", ""), fields.get("wikidata_id", ""),
                 fields.get("nafnid_id", ""), fields.get("geo_match_score"),
                 fields.get("notes", ""), fields.get("sources", "")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def update_place(place_pk: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                f"UPDATE places SET {cols}, updated_at = datetime('now') WHERE place_pk = ?",
                (*fields.values(), place_pk),
            )
    finally:
        conn.close()


def update_place_geocoding(place_pk: int, coordinates_lat: float | None = None,
                             coordinates_long: float | None = None, wikidata_id: str | None = None,
                             geo_match_score: float | None = None) -> None:
    """Used by 04_lookup_coords.py -- only fills currently-blank fields,
    matching that script's existing 'never clobber an existing value' rule."""
    conn = get_connection()
    try:
        with conn:
            row = conn.execute(
                "SELECT coordinates_lat, coordinates_long, wikidata_id FROM places WHERE place_pk = ?",
                (place_pk,),
            ).fetchone()
            if row is None:
                return
            new_lat = row["coordinates_lat"] if row["coordinates_lat"] is not None else coordinates_lat
            new_long = row["coordinates_long"] if row["coordinates_long"] is not None else coordinates_long
            new_wd = row["wikidata_id"] if row["wikidata_id"] else (wikidata_id or "")
            conn.execute(
                """UPDATE places SET coordinates_lat=?, coordinates_long=?, wikidata_id=?,
                   geo_match_score=COALESCE(?, geo_match_score), updated_at=datetime('now')
                   WHERE place_pk = ?""",
                (new_lat, new_long, new_wd, geo_match_score, place_pk),
            )
    finally:
        conn.close()


def reconcile_place_wikidata(place_pk: int) -> dict | None:
    """Replaces 04b_propagate_corrections.py's manual annotate/apply flow:
    called automatically when a provisional place's canonical_name/
    variant_names changes. Looks up a canonical place by the same
    multi-strategy find_place() logic and fills wikidata_id if currently
    blank. Returns the matched canonical row, or None if no match."""
    conn = get_connection()
    try:
        place = get_place_by_pk(place_pk, conn)
        if place is None or place["wikidata_id"]:
            return None
        match = find_place(place["canonical_name"], variant_names=split_variants(place["variant_names"]),
                            conn=conn)
        if match and match["wikidata_id"]:
            with conn:
                conn.execute(
                    "UPDATE places SET wikidata_id=?, updated_at=datetime('now') WHERE place_pk=?",
                    (match["wikidata_id"], place_pk),
                )
        return match
    finally:
        conn.close()


def _promote_places_batch_impl(place_pks: list[int]) -> dict:
    conn = get_connection()
    added, skipped_existing = [], []
    try:
        with conn:
            for pk in place_pks:
                row = conn.execute("SELECT status FROM places WHERE place_pk = ?", (pk,)).fetchone()
                if row is None:
                    continue
                if row["status"] == "canonical":
                    skipped_existing.append(pk)
                    continue
                conn.execute(
                    "UPDATE places SET status='canonical', updated_at=datetime('now') WHERE place_pk = ?",
                    (pk,),
                )
                added.append(pk)
    finally:
        conn.close()
    return {"added": added, "skipped_existing": skipped_existing}


def promote_places_batch(place_pks: list[int]) -> dict:
    return with_undo(f"Promoted {len(place_pks)} place(s) to authority",
                      _promote_places_batch_impl, place_pks)


PLACE_UNION_FIELDS = ["variant_names", "notes", "sources"]
PLACE_FIRST_NONBLANK_FIELDS = ["place_type", "region", "district", "modern_equivalent", "wikidata_id"]


def _merge_places_impl(survivor_pk: int, dropped_pks: list[int]) -> dict:
    conn = get_connection()
    try:
        with conn:
            rows = [dict(conn.execute(
                "SELECT * FROM places WHERE place_pk = ?", (pk,)
            ).fetchone()) for pk in [survivor_pk, *dropped_pks]]
            survivor_row = rows[0]
            merged = dict(survivor_row)

            for field in PLACE_UNION_FIELDS:
                vals = [r.get(field, "") for r in rows]
                if field == "variant_names":
                    vals = vals + [r.get("canonical_name", "") for r in rows[1:]]
                merged[field] = _union_semicolon(vals)
            survivor_name_key = merged["canonical_name"].strip().lower()
            merged["variant_names"] = ";".join(
                v for v in merged["variant_names"].split(";")
                if v.strip().lower() != survivor_name_key
            )
            for field in PLACE_FIRST_NONBLANK_FIELDS:
                merged[field] = _first_nonblank([r.get(field, "") for r in rows])
            lats = [r.get("coordinates_lat") for r in rows if r.get("coordinates_lat") is not None]
            longs = [r.get("coordinates_long") for r in rows if r.get("coordinates_long") is not None]
            merged["coordinates_lat"] = lats[0] if lats else None
            merged["coordinates_long"] = longs[0] if longs else None
            merged["merged_volumes"] = _merged_volumes_union(rows)

            conn.execute(
                """UPDATE places SET variant_names=?, notes=?, sources=?, place_type=?, region=?,
                   district=?, modern_equivalent=?, wikidata_id=?, coordinates_lat=?,
                   coordinates_long=?, merged_volumes=?, updated_at=datetime('now') WHERE place_pk = ?""",
                (merged["variant_names"], merged["notes"], merged["sources"], merged["place_type"],
                 merged["region"], merged["district"], merged["modern_equivalent"],
                 merged["wikidata_id"], merged["coordinates_lat"], merged["coordinates_long"],
                 merged["merged_volumes"], survivor_pk),
            )
            for pk in dropped_pks:
                conn.execute("UPDATE charter_places SET place_pk=? WHERE place_pk=?", (survivor_pk, pk))
                conn.execute("UPDATE charter_places SET review_match_place_pk=? WHERE review_match_place_pk=?",
                             (survivor_pk, pk))
                conn.execute("UPDATE review_queue_items SET match_pk=? WHERE match_pk=? AND entity_type='place'",
                             (survivor_pk, pk))
                conn.execute("UPDATE review_queue_items SET outcome_pk=? WHERE outcome_pk=? AND entity_type='place'",
                             (survivor_pk, pk))
                conn.execute("UPDATE place_duplicate_candidates SET place_pk=? WHERE place_pk=?",
                             (survivor_pk, pk))
                conn.execute("DELETE FROM places WHERE place_pk = ?", (pk,))
    finally:
        conn.close()
    return {"survivor_pk": survivor_pk, "dropped_pks": dropped_pks}


def merge_places(survivor_pk: int, dropped_pks: list[int]) -> dict:
    if len(dropped_pks) < 1:
        raise ValueError("Need at least one id to merge into the survivor.")
    return with_undo(f"Merged {len(dropped_pks)} place(s) into place_pk={survivor_pk}",
                      _merge_places_impl, survivor_pk, dropped_pks)


def _merge_into_authority_impl(entity_type: str, provisional_pk: int, authority_pk: int) -> dict:
    """A provisional row is being confirmed as the SAME real-world entity as
    an already-canonical row -- relink every reference to the canonical pk
    and drop the provisional row entirely (never promoted on its own).
    Different operation from merge_persons/merge_places, which unite two
    not-yet-promoted rows; this one's survivor is always the canonical side."""
    if entity_type == "person":
        return _merge_persons_impl(authority_pk, [provisional_pk])
    elif entity_type == "place":
        return _merge_places_impl(authority_pk, [provisional_pk])
    raise ValueError(f"Unknown entity_type: {entity_type!r}")


def merge_into_authority(entity_type: str, provisional_pk: int, authority_pk: int) -> dict:
    return with_undo(
        f"Merged provisional {entity_type} {provisional_pk} into authority {authority_pk}",
        _merge_into_authority_impl, entity_type, provisional_pk, authority_pk,
    )


def search_authority(entity_type: str, name: str, limit: int = 3) -> list[dict]:
    """On-demand fuzzy lookup against the canonical table for the Compare
    Panel's New-Entities entry point (these rows have no precomputed
    candidate, unlike Review Queue/Final Review rows)."""
    from rapidfuzz import fuzz, process
    conn = get_connection()
    try:
        table = "persons" if entity_type == "person" else "places"
        df = pd.read_sql_query(f"SELECT * FROM {table} WHERE status = 'canonical'", conn)
        if df.empty:
            return []
        names = df["canonical_name"].tolist()
        matches = process.extract(name, names, scorer=fuzz.token_sort_ratio, limit=limit)
        out = []
        for matched_name, score, idx in matches:
            row = df.iloc[idx].to_dict()
            row["_match_score"] = score
            out.append(row)
        return out
    finally:
        conn.close()


def get_volumes() -> list[int]:
    """Every volume with at least one charter, provisional person, or
    provisional place -- replaces review_app.py's old available_volumes()
    (which globbed *_review_queue.csv filenames)."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT DISTINCT volume AS v FROM charters
            UNION SELECT DISTINCT source_volume AS v FROM persons WHERE source_volume IS NOT NULL
            UNION SELECT DISTINCT source_volume AS v FROM places WHERE source_volume IS NOT NULL
            ORDER BY 1
        """).fetchall()
        return [r["v"] for r in rows]
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Charters + junction tables
# ═══════════════════════════════════════════════════════════════════════════

def get_charters(volume: int | None = None, has_review: bool | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        q, params = "SELECT * FROM charters WHERE 1=1", []
        if volume is not None:
            q += " AND volume = ?"; params.append(volume)
        if has_review is True:
            q += " AND (has_parse_error=1 OR has_review_persons=1 OR has_review_places=1)"
        elif has_review is False:
            q += " AND has_parse_error=0 AND has_review_persons=0 AND has_review_places=0"
        q += " ORDER BY volume, sequence"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        conn.close()


def get_charter_persons(charter_pk: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM charter_persons WHERE charter_pk = ? ORDER BY ordinal", (charter_pk,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def get_charter_places(charter_pk: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM charter_places WHERE charter_pk = ? ORDER BY ordinal", (charter_pk,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def create_charter(volume: int, sequence: int, conn: sqlite3.Connection | None = None, **fields) -> int:
    own = conn is None
    conn = conn or get_connection()
    try:
        charter_id_placeholder = fields.pop("charter_id_placeholder", None) or f"c_vol{volume:02d}_seq{sequence:04d}"
        cur = conn.execute(
            """INSERT INTO charters
               (charter_id_placeholder, volume, sequence, shelfmark_auto, di_reference, date,
                di_year, date_uncertain, date_header, doc_type, subject, outcome, scribe,
                scribe_source, seal_info, language, notes, has_parse_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (charter_id_placeholder, volume, sequence, fields.get("shelfmark_auto", ""),
             fields.get("di_reference", ""), fields.get("date", ""),
             to_int_or_none(charter_year(fields.get("date", ""))),
             fields.get("date_uncertain", ""), fields.get("date_header", ""),
             fields.get("doc_type", ""), fields.get("subject", ""), fields.get("outcome", ""),
             fields.get("scribe", ""), fields.get("scribe_source", ""), fields.get("seal_info", ""),
             fields.get("language", ""), fields.get("notes", ""),
             int(bool(fields.get("has_parse_error", 0)))),
        )
        if own:
            conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def to_int_or_none(s):
    try:
        return int(s) if s not in (None, "") else None
    except (TypeError, ValueError):
        return None


def add_charter_person(charter_pk: int, ordinal: int, role_category: str, extracted_name: str,
                         person_pk: int | None = None, resolution_state: str = "resolved",
                         review_match_person_pk: int | None = None, match_score: float | None = None,
                         qualifier: str = "", conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO charter_persons
               (charter_pk, ordinal, role_category, qualifier, extracted_name, person_pk,
                match_score, resolution_state, review_match_person_pk)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (charter_pk, ordinal, role_category, qualifier, extracted_name, person_pk,
             match_score, resolution_state, review_match_person_pk),
        )
        if own:
            conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def add_charter_place(charter_pk: int, ordinal: int, role: str, extracted_name: str,
                        place_pk: int | None = None, resolution_state: str = "resolved",
                        review_match_place_pk: int | None = None, match_score: float | None = None,
                        region: str = "", conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO charter_places
               (charter_pk, ordinal, role, region, extracted_name, place_pk, match_score,
                resolution_state, review_match_place_pk)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (charter_pk, ordinal, role, region, extracted_name, place_pk, match_score,
             resolution_state, review_match_place_pk),
        )
        if own:
            conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def update_charter(volume: int, sequence: int, **fields) -> None:
    """Charters tab's small set of genuinely-editable columns (date,
    doc_type, subject, outcome, scribe, scribe_source, seal_info, language,
    date_uncertain) -- never grantor_id/recipient_id/persons_by_role/etc.,
    which are derived at export time, not stored."""
    if not fields:
        return
    if "date" in fields and "di_year" not in fields:
        fields["di_year"] = to_int_or_none(charter_year(fields["date"]))
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                f"UPDATE charters SET {cols}, updated_at = datetime('now') "
                f"WHERE volume = ? AND sequence = ?",
                (*fields.values(), volume, sequence),
            )
    finally:
        conn.close()


def rescan_review_flags(volume: int | None = None) -> dict:
    """Replaces 05b_rescan_flags.py -- recomputes has_review_persons/
    has_review_places/has_parse_error from the actual junction-table
    resolution_state, in-process (no subprocess, no --vol/--csv path
    divergence risk the old script had)."""
    conn = get_connection()
    try:
        with conn:
            vol_clause = "AND volume = ?" if volume is not None else ""
            params = (volume,) if volume is not None else ()
            conn.execute(
                f"""UPDATE charters SET has_review_persons = (
                        SELECT COUNT(*) FROM charter_persons
                        WHERE charter_persons.charter_pk = charters.charter_pk
                          AND resolution_state = 'pending_review'
                    ) > 0
                    WHERE 1=1 {vol_clause}""", params,
            )
            conn.execute(
                f"""UPDATE charters SET has_review_places = (
                        SELECT COUNT(*) FROM charter_places
                        WHERE charter_places.charter_pk = charters.charter_pk
                          AND resolution_state = 'pending_review'
                    ) > 0
                    WHERE 1=1 {vol_clause}""", params,
            )
            n_persons = conn.execute(
                f"SELECT COUNT(*) c FROM charters WHERE has_review_persons=1 {vol_clause}", params
            ).fetchone()["c"]
            n_places = conn.execute(
                f"SELECT COUNT(*) c FROM charters WHERE has_review_places=1 {vol_clause}", params
            ).fetchone()["c"]
            n_errors = conn.execute(
                f"SELECT COUNT(*) c FROM charters WHERE has_parse_error=1 {vol_clause}", params
            ).fetchone()["c"]
    finally:
        conn.close()
    return {"has_review_persons": n_persons, "has_review_places": n_places, "has_parse_error": n_errors}


# ═══════════════════════════════════════════════════════════════════════════
# Review queue
# ═══════════════════════════════════════════════════════════════════════════

def create_review_item(entity_type: str, charter_pk: int, extracted_name: str, match_pk: int,
                        score: float | None, charter_person_pk: int | None = None,
                        charter_place_pk: int | None = None, role_category: str = "",
                        role: str = "", closest_match: str = "", charter_date: str = "") -> int:
    """Creates an open review_queue_items row for a pending_review charter_persons/
    charter_places entry -- called by 05_export_csvs.py right after add_charter_person/
    add_charter_place for any row it marks pending_review, so a freshly-processed
    volume's ambiguous matches are visible in the Review Queue tab immediately
    (not just already-migrated volumes, whose review_queue_items came from
    migrate_to_sqlite.py's one-time positional-join pass)."""
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO review_queue_items
                   (entity_type, charter_person_pk, charter_place_pk, charter_pk, extracted_name,
                    closest_match, match_pk, score, role_category, role, charter_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entity_type, charter_person_pk, charter_place_pk, charter_pk, extracted_name,
                 closest_match, match_pk, score, role_category, role, charter_date),
            )
            return cur.lastrowid
    finally:
        conn.close()


def get_open_review_items(volume: int | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        q = """SELECT rq.*, c.volume AS charter_volume
               FROM review_queue_items rq JOIN charters c ON c.charter_pk = rq.charter_pk
               WHERE rq.status = 'open'"""
        params = []
        if volume is not None:
            q += " AND c.volume = ?"; params.append(volume)
        q += " ORDER BY rq.review_item_pk"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        conn.close()


def get_resolved_review_items(volume: int | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        q = """SELECT rq.*, c.volume AS charter_volume
               FROM review_queue_items rq JOIN charters c ON c.charter_pk = rq.charter_pk
               WHERE rq.status = 'resolved'"""
        params = []
        if volume is not None:
            q += " AND c.volume = ?"; params.append(volume)
        q += " ORDER BY rq.review_item_pk"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        conn.close()


def set_review_decision(review_item_pk: int, decision: str) -> None:
    """Records decision without applying it yet -- mirrors the old app's
    inline data_editor cell edit (Save writes the decision column; a
    separate 'Resolve decided rows' action actually applies it)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE review_queue_items SET decision = ? WHERE review_item_pk = ?",
                (decision, review_item_pk),
            )
    finally:
        conn.close()


def _apply_review_decision_impl(review_item_pk: int, new_entity_fields: dict | None) -> dict:
    conn = get_connection()
    try:
        with conn:
            item = conn.execute(
                "SELECT * FROM review_queue_items WHERE review_item_pk = ?", (review_item_pk,)
            ).fetchone()
            if item is None:
                raise ValueError(f"No review_queue_items row with pk={review_item_pk}")
            if item["decision"] not in ("accept", "reject"):
                return {"skipped": True, "reason": "decision is blank"}

            entity_type = item["entity_type"]
            charter = conn.execute(
                "SELECT * FROM charters WHERE charter_pk = ?", (item["charter_pk"],)
            ).fetchone()

            if item["decision"] == "accept":
                outcome_pk = item["match_pk"]
            else:
                fields = new_entity_fields or {}
                legacy_id = f"REJECTED-{review_item_pk}"
                if entity_type == "person":
                    outcome_pk = insert_provisional_person(
                        charter["volume"], legacy_id,
                        fields.get("canonical_name", item["extracted_name"]),
                        occupation=fields.get("occupation", item["role_category"]),
                        title=fields.get("title", item["role"]),
                        floruit_start=to_int_or_none(charter_year(charter["date"])),
                        floruit_end=to_int_or_none(charter_year(charter["date"])),
                        sources=fields.get("sources", f"DI vol.{charter['volume']} seq.{charter['sequence']} "
                                                       f"| {charter['di_reference']}"),
                    )
                else:
                    outcome_pk = insert_provisional_place(
                        charter["volume"], legacy_id,
                        fields.get("canonical_name", item["extracted_name"]),
                        region=fields.get("region", item["role"]),
                        sources=fields.get("sources", f"DI vol.{charter['volume']} seq.{charter['sequence']} "
                                                       f"| {charter['di_reference']}"),
                    )

            if entity_type == "person":
                conn.execute(
                    """UPDATE charter_persons SET person_pk=?, resolution_state='resolved',
                       review_match_person_pk=NULL WHERE charter_person_pk = ?""",
                    (outcome_pk, item["charter_person_pk"]),
                )
            else:
                conn.execute(
                    """UPDATE charter_places SET place_pk=?, resolution_state='resolved',
                       review_match_place_pk=NULL WHERE charter_place_pk = ?""",
                    (outcome_pk, item["charter_place_pk"]),
                )

            conn.execute(
                """UPDATE review_queue_items SET status='resolved', outcome_pk=?,
                   resolved_at=datetime('now') WHERE review_item_pk = ?""",
                (outcome_pk, review_item_pk),
            )
    finally:
        conn.close()
    rescan_review_flags(charter["volume"])
    return {"decision": item["decision"], "outcome_pk": outcome_pk}


def apply_review_decision(review_item_pk: int, new_entity_fields: dict | None = None) -> dict:
    """Applies an already-recorded accept/reject decision -- replaces
    resolve_review_queue.py's whole positional-join block, since
    review_queue_items has a direct FK to the exact charter_persons/
    charter_places row (see schema.sql)."""
    return with_undo(f"Resolved review queue item {review_item_pk}",
                      _apply_review_decision_impl, review_item_pk, new_entity_fields)


def apply_review_decisions_for_volume(volume: int) -> dict:
    """Convenience wrapper: applies every open, non-blank-decision item for
    a volume in one undo-tracked batch (mirrors the old app's 'Resolve
    decided rows' button acting on the whole queue at once)."""
    conn = get_connection()
    try:
        pks = [r["review_item_pk"] for r in conn.execute(
            """SELECT rq.review_item_pk FROM review_queue_items rq
               JOIN charters c ON c.charter_pk = rq.charter_pk
               WHERE c.volume = ? AND rq.status = 'open' AND rq.decision IN ('accept', 'reject')""",
            (volume,),
        ).fetchall()]
    finally:
        conn.close()

    def _apply_all():
        results = []
        for pk in pks:
            results.append(_apply_review_decision_impl(pk, None))
        return {"processed": len(results), "results": results}

    if not pks:
        return {"processed": 0, "results": []}
    return with_undo(f"Resolved {len(pks)} review queue item(s) for vol{volume:02d}", _apply_all)


# ═══════════════════════════════════════════════════════════════════════════
# Duplicate candidates
# ═══════════════════════════════════════════════════════════════════════════

def get_person_duplicate_candidates(decision: str | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        q = """SELECT pdc.*,
                      pa.display_id AS a_display_id, pa.canonical_name AS a_canonical_name,
                      pa.occupation AS a_occupation, pa.title AS a_title,
                      pa.floruit_start AS a_floruit_start, pa.floruit_end AS a_floruit_end,
                      (CASE WHEN pa.status='canonical' THEN 'authority'
                            ELSE 'vol' || printf('%02d', pa.source_volume) END) AS a_source,
                      pb.display_id AS b_display_id, pb.canonical_name AS b_canonical_name,
                      pb.occupation AS b_occupation, pb.title AS b_title,
                      pb.floruit_start AS b_floruit_start, pb.floruit_end AS b_floruit_end,
                      (CASE WHEN pb.status='canonical' THEN 'authority'
                            ELSE 'vol' || printf('%02d', pb.source_volume) END) AS b_source
               FROM person_duplicate_candidates pdc
               JOIN persons pa ON pa.person_pk = pdc.person_a_pk
               JOIN persons pb ON pb.person_pk = pdc.person_b_pk
               WHERE 1=1"""
        params = []
        if decision is not None:
            q += " AND pdc.decision = ?"; params.append(decision)
        q += " ORDER BY pdc.name_score DESC, pdc.candidate_pk"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        conn.close()


def upsert_person_duplicate_candidates(rows: list[dict]) -> dict:
    """rows: [{"person_a_pk", "person_b_pk", "name_score", "date_status",
    "classification", "confidence"}, ...]. Never overwrites a non-blank
    decision (fixes 07_find_person_duplicates.py's old bug of silently
    wiping a human's recorded same/different decision on every re-run)."""
    conn = get_connection()
    inserted, updated = 0, 0
    try:
        with conn:
            for r in rows:
                a, b = sorted((r["person_a_pk"], r["person_b_pk"]))
                # cursor.rowcount after INSERT ... ON CONFLICT DO UPDATE can't
                # distinguish "fresh insert" from "conflict, update applied"
                # (both report 1) -- check existence first instead.
                existed = conn.execute(
                    "SELECT 1 FROM person_duplicate_candidates WHERE person_a_pk=? AND person_b_pk=?",
                    (a, b),
                ).fetchone() is not None
                conn.execute(
                    """INSERT INTO person_duplicate_candidates
                       (person_a_pk, person_b_pk, name_score, date_status, classification, confidence)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(person_a_pk, person_b_pk) DO UPDATE SET
                         name_score=excluded.name_score, date_status=excluded.date_status,
                         classification=excluded.classification, confidence=excluded.confidence
                       WHERE person_duplicate_candidates.decision = ''""",
                    (a, b, r["name_score"], r.get("date_status", ""),
                     r.get("classification", ""), r.get("confidence", "")),
                )
                if existed:
                    updated += 1
                else:
                    inserted += 1
    finally:
        conn.close()
    return {"inserted": inserted, "updated_or_unchanged": updated}


def record_person_duplicate_decision(candidate_pk: int, decision: str) -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE person_duplicate_candidates SET decision=?, decided_at=datetime('now') "
                "WHERE candidate_pk = ?", (decision, candidate_pk),
            )
    finally:
        conn.close()


def get_place_duplicate_candidates(volume: int | None = None, decision: str | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        q = """SELECT pdc.*, p.display_id, p.canonical_name AS place_canonical_name, p.source_volume
               FROM place_duplicate_candidates pdc JOIN places p ON p.place_pk = pdc.place_pk
               WHERE 1=1"""
        params = []
        if volume is not None:
            q += " AND p.source_volume = ?"; params.append(volume)
        if decision is not None:
            q += " AND pdc.decision = ?"; params.append(decision)
        q += " ORDER BY pdc.name_score DESC, pdc.candidate_pk"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        conn.close()


def replace_place_duplicate_candidates(volume: int, rows: list[dict]) -> dict:
    """Replaces every candidate row for the given volume's places (04a's
    re-run behavior -- unlike persons, no confirmed-same signal exists for
    places yet, so there's nothing to preserve across a re-run)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """DELETE FROM place_duplicate_candidates
                   WHERE place_pk IN (SELECT place_pk FROM places WHERE source_volume = ?)""",
                (volume,),
            )
            for r in rows:
                conn.execute(
                    """INSERT INTO place_duplicate_candidates
                       (place_pk, di_name, di_sysla_given, di_place_type, di_region, wikidata_status,
                        candidate_rank, name_score, distance_km, flag, match_sources, candidate_name,
                        candidate_nafnid, candidate_hreppur, candidate_sysla, candidate_lat, candidate_lng)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["place_pk"], r.get("di_name", ""), r.get("di_sysla_given", ""),
                     r.get("di_place_type", ""), r.get("di_region", ""), r.get("wikidata_status", ""),
                     r.get("candidate_rank"), r.get("name_score"), r.get("distance_km"),
                     r.get("flag", ""), r.get("match_sources", ""), r.get("candidate_name", ""),
                     r.get("candidate_nafnid", ""), r.get("candidate_hreppur", ""),
                     r.get("candidate_sysla", ""), r.get("candidate_lat"), r.get("candidate_lng")),
                )
    finally:
        conn.close()
    return {"inserted": len(rows)}


def record_place_duplicate_decision(candidate_pk: int, decision: str) -> None:
    """On decision='same', backfills places.nafnid_id from this candidate's
    candidate_nafnid if currently blank -- mirrors how a confirmed identity
    match already propagates into wikidata_id elsewhere."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE place_duplicate_candidates SET decision=? WHERE candidate_pk = ?",
                (decision, candidate_pk),
            )
            if decision == "same":
                cand = conn.execute(
                    "SELECT place_pk, candidate_nafnid FROM place_duplicate_candidates WHERE candidate_pk = ?",
                    (candidate_pk,),
                ).fetchone()
                if cand and cand["candidate_nafnid"]:
                    conn.execute(
                        """UPDATE places SET nafnid_id = ?, updated_at = datetime('now')
                           WHERE place_pk = ? AND (nafnid_id = '' OR nafnid_id IS NULL)""",
                        (cand["candidate_nafnid"], cand["place_pk"]),
                    )
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Final Review aggregation (replaces promote_to_authority.py's file-based version)
# ═══════════════════════════════════════════════════════════════════════════

def _person_duplicate_status(person_pks: set[int], conn: sqlite3.Connection) -> dict[int, tuple]:
    """{pk: (status, detail, other_pk, candidate_pk)}. other_pk/candidate_pk
    let the UI's Compare panel jump straight to the known candidate instead
    of re-parsing the human-readable `detail` string."""
    result: dict[int, tuple] = {}
    rows = conn.execute("SELECT * FROM person_duplicate_candidates").fetchall()
    for r in rows:
        for this_pk, other_pk in ((r["person_a_pk"], r["person_b_pk"]),
                                    (r["person_b_pk"], r["person_a_pk"])):
            if this_pk not in person_pks:
                continue
            other = get_person_by_pk(other_pk, conn)
            other_name = other["canonical_name"] if other else "?"
            if r["decision"] == "same":
                result[this_pk] = ("blocked", f"Confirmed duplicate of {other_pk} ({other_name})",
                                   other_pk, r["candidate_pk"])
            elif this_pk not in result:
                result[this_pk] = ("warning", f"Unresolved possible duplicate of {other_pk} ({other_name})",
                                   other_pk, r["candidate_pk"])
    return result


def _place_duplicate_status(place_pks: set[int], conn: sqlite3.Connection) -> dict[int, tuple]:
    """No confirmed-same signal blocks promotion for places today (mirrors
    the old promote_to_authority.py's _place_duplicate_status) -- 'blocked'
    is reachable now that place_duplicate_candidates has a decision column,
    but this stays warn-only until a place-side hard-block rule is designed.
    {pk: (status, detail, candidate_pk)}."""
    result: dict[int, tuple] = {}
    rows = conn.execute("SELECT * FROM place_duplicate_candidates WHERE decision != 'different'").fetchall()
    for r in rows:
        pk = r["place_pk"]
        if pk in place_pks and pk not in result:
            result[pk] = ("warning", f"Unreviewed nafnid candidate: {r['candidate_name']}", r["candidate_pk"])
    return result


def get_final_review_candidates(volumes: list[int] | None = None) -> list[dict]:
    """Everything currently review_status=='add' and status=='provisional'
    (not yet promoted), across the given volumes (or all, if None),
    annotated with duplicate_status. Pure read, safe on every render."""
    conn = get_connection()
    try:
        vol_clause = ""
        params: list = []
        if volumes:
            vol_clause = f" AND source_volume IN ({','.join('?' * len(volumes))})"
            params = list(volumes)
        person_rows = conn.execute(
            f"SELECT * FROM persons WHERE status='provisional' AND review_status='add'{vol_clause}",
            params,
        ).fetchall()
        place_rows = conn.execute(
            f"SELECT * FROM places WHERE status='provisional' AND review_status='add'{vol_clause}",
            params,
        ).fetchall()

        dup_status_p = _person_duplicate_status({r["person_pk"] for r in person_rows}, conn)
        dup_status_pl = _place_duplicate_status({r["place_pk"] for r in place_rows}, conn)

        out = []
        for r in person_rows:
            status, detail, other_pk, cand_pk = dup_status_p.get(r["person_pk"], ("none", "", None, None))
            out.append({
                "volume": r["source_volume"], "entity_type": "person", "pk": r["person_pk"],
                "id": r["display_id"], "canonical_name": r["canonical_name"],
                "occupation": r["occupation"], "title": r["title"],
                "floruit_start": r["floruit_start"], "floruit_end": r["floruit_end"],
                "sources": r["sources"], "duplicate_status": status, "duplicate_detail": detail,
                "duplicate_other_pk": other_pk, "duplicate_candidate_pk": cand_pk,
            })
        for r in place_rows:
            status, detail, cand_pk = dup_status_pl.get(r["place_pk"], ("none", "", None))
            out.append({
                "volume": r["source_volume"], "entity_type": "place", "pk": r["place_pk"],
                "id": r["display_id"], "canonical_name": r["canonical_name"],
                "region": r["region"], "place_type": r["place_type"],
                "coordinates_lat": r["coordinates_lat"], "coordinates_long": r["coordinates_long"],
                "sources": r["sources"], "duplicate_status": status, "duplicate_detail": detail,
                "duplicate_other_pk": None, "duplicate_candidate_pk": cand_pk,
            })
        return out
    finally:
        conn.close()


def _promote_all_impl(volumes: list[int] | None) -> dict:
    candidates = get_final_review_candidates(volumes)
    person_pks = [c["pk"] for c in candidates if c["entity_type"] == "person" and c["duplicate_status"] != "blocked"]
    place_pks = [c["pk"] for c in candidates if c["entity_type"] == "place" and c["duplicate_status"] != "blocked"]
    blocked_persons = [c["pk"] for c in candidates if c["entity_type"] == "person" and c["duplicate_status"] == "blocked"]
    blocked_places = [c["pk"] for c in candidates if c["entity_type"] == "place" and c["duplicate_status"] == "blocked"]
    persons_result = _promote_persons_batch_impl(person_pks)
    places_result = _promote_places_batch_impl(place_pks)
    persons_result["skipped_blocked"] = blocked_persons
    places_result["skipped_blocked"] = blocked_places
    return {"persons": persons_result, "places": places_result}


def promote_all(volumes: list[int] | None = None) -> dict:
    """Recomputes eligibility itself at the moment of promotion (never
    trusts a possibly-stale list handed in from the UI) -- the hard-block
    gate. Replaces promote_to_authority.py's promote_all()."""
    return with_undo("Added eligible new entities to the authority file", _promote_all_impl, volumes)
