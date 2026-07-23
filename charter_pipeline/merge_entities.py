"""
Merge two or more provisionally-new person/place rows that the pipeline
minted as separate entities but that are really the same real-world
person/place, split apart across charters because fuzzy matching didn't
catch the spelling difference within one volume.

Unlike 07_find_person_duplicates.py (flag-only), this rewrites data: the
New Entities review CSV, the resolved-entities JSON, and the per-charter
export CSV are all updated so every reference to the dropped id(s) points
at the surviving id instead.

New-entity IDs are only unique *within* a volume (03_resolve_entities.py
never checks other volumes' pending ids against each other), so a merge is
always scoped to a single volume's files.
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import ENTITIES_DIR, REVIEW_DIR

PERSON_UNION_FIELDS = ["variant_names", "notes", "sources", "associated_places"]
PERSON_FIRST_NONBLANK_FIELDS = [
    "patronymic", "occupation", "title", "gender", "review_status", "wikidata_id",
]

PLACE_UNION_FIELDS = ["variant_names", "notes", "sources"]
PLACE_FIRST_NONBLANK_FIELDS = [
    "place_type", "coordinates_lat", "coordinates_long",
    "region", "district", "modern_equivalent", "review_status", "wikidata_id",
]


def pick_survivor(ids: list[str]) -> str:
    """Lowest numeric suffix = first-minted (03_resolve_entities.py's next_id()
    allocates sequentially), e.g. ["p022", "p014"] -> "p014"."""
    return min(ids, key=lambda i: int(re.sub(r"\D", "", i) or 0))


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


def _merge_rows(
    rows: list[dict], survivor_id: str, id_col: str, name_col: str,
    union_fields: list[str], first_fields: list[str], floruit: bool = False,
) -> dict:
    survivor_row = next(r for r in rows if r[id_col] == survivor_id)
    cols = set(survivor_row.keys())
    merged = dict(survivor_row)

    for field in union_fields:
        if field not in cols:
            continue
        vals = [r.get(field, "") for r in rows]
        if field == "variant_names":
            vals = vals + [r.get(name_col, "") for r in rows if r[id_col] != survivor_id]
        merged[field] = _union_semicolon(vals)

    # never let the survivor's own canonical name sit in its own variant list
    if "variant_names" in merged and merged["variant_names"]:
        survivor_name_key = merged.get(name_col, "").strip().lower()
        merged["variant_names"] = ";".join(
            v for v in merged["variant_names"].split(";")
            if v.strip().lower() != survivor_name_key
        )

    for field in first_fields:
        if field not in cols:
            continue
        merged[field] = _first_nonblank([r.get(field, "") for r in rows])

    if floruit and "floruit_start" in cols and "floruit_end" in cols:
        starts = [r.get("floruit_start", "") for r in rows if (r.get("floruit_start") or "").strip()]
        ends = [r.get("floruit_end", "") for r in rows if (r.get("floruit_end") or "").strip()]
        merged["floruit_start"] = min(starts, key=int) if starts else ""
        merged["floruit_end"] = max(ends, key=int) if ends else ""

    return merged


def merge_new_entities(vol: int, entity_type: str, ids: list[str]) -> dict:
    """
    entity_type: "person" or "place". ids: >=2 provisional ids (e.g.
    ["p014", "p022"]) from the same volume's New Entities list to collapse
    into one. Returns a summary dict for display in the review app.
    """
    if len(ids) < 2:
        raise ValueError("Need at least two ids to merge.")
    if entity_type not in ("person", "place"):
        raise ValueError(f"Unknown entity_type: {entity_type!r}")

    prefix = f"vol{vol:02d}"
    id_col = "person_id" if entity_type == "person" else "place_id"
    csv_name = "persons_new" if entity_type == "person" else "places_new"
    csv_path = REVIEW_DIR / f"{prefix}_{csv_name}.csv"
    resolved_path = ENTITIES_DIR / f"{prefix}_resolved_entities.json"
    charters_path = REVIEW_DIR / f"{prefix}_charters.csv"

    survivor_id = pick_survivor(ids)
    dropped_ids = [i for i in ids if i != survivor_id]

    # 1. Merge the review-list CSV row.
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    rows = df[df[id_col].isin(ids)].to_dict("records")
    missing = set(ids) - {r[id_col] for r in rows}
    if missing:
        raise ValueError(f"IDs not found in {csv_path.name}: {sorted(missing)}")

    if entity_type == "person":
        merged = _merge_rows(rows, survivor_id, id_col, "canonical_name",
                              PERSON_UNION_FIELDS, PERSON_FIRST_NONBLANK_FIELDS, floruit=True)
    else:
        merged = _merge_rows(rows, survivor_id, id_col, "canonical_name",
                              PLACE_UNION_FIELDS, PLACE_FIRST_NONBLANK_FIELDS)

    df = df[~df[id_col].isin(dropped_ids)]
    df.loc[df[id_col] == survivor_id, list(merged.keys())] = list(merged.values())
    df.to_csv(csv_path, index=False)

    # 2. Relink the resolved-entities JSON (source of truth if 05 is re-run).
    n_resolved = 0
    if resolved_path.exists():
        with open(resolved_path, encoding="utf-8") as f:
            charters = json.load(f)
        list_key = "resolved_persons" if entity_type == "person" else "resolved_locations"
        new_list_key = "new_persons" if entity_type == "person" else "new_places"
        for ch in charters:
            for item in ch.get(list_key, []):
                if item.get(id_col) in dropped_ids:
                    item[id_col] = survivor_id
                    n_resolved += 1
            if new_list_key in ch:
                ch[new_list_key] = list(dict.fromkeys(
                    survivor_id if x in dropped_ids else x for x in ch[new_list_key]
                ))
        with open(resolved_path, "w", encoding="utf-8") as f:
            json.dump(charters, f, ensure_ascii=False, indent=2)

    # 3. Relink the per-charter export CSV (what 06_merge_into_xlsx.py reads).
    n_charters = 0
    if charters_path.exists():
        cdf = pd.read_csv(charters_path, dtype=str).fillna("")
        touched_rows = set()

        id_cols = (["grantor_id", "recipient_id"] if entity_type == "person"
                   else ["location_written_id", "location_hearing_id"])
        for col in id_cols:
            if col in cdf.columns:
                mask = cdf[col].isin(dropped_ids)
                touched_rows.update(cdf.index[mask])
                cdf.loc[mask, col] = survivor_id

        if entity_type == "place" and "locations_mentioned_ids" in cdf.columns:
            def _relink_list(s):
                parts = [survivor_id if p.strip() in dropped_ids else p.strip()
                         for p in s.split(";") if p.strip()]
                return "; ".join(dict.fromkeys(parts))

            mask = cdf["locations_mentioned_ids"].apply(
                lambda s: any(p.strip() in dropped_ids for p in s.split(";"))
            )
            touched_rows.update(cdf.index[mask])
            cdf["locations_mentioned_ids"] = cdf["locations_mentioned_ids"].apply(_relink_list)

        if entity_type == "person" and "persons_by_role" in cdf.columns:
            def _relink_brackets(s):
                out = s
                for d in dropped_ids:
                    out = out.replace(f"[{d}]", f"[{survivor_id}]")
                return out

            mask = cdf["persons_by_role"].apply(
                lambda s: any(f"[{d}]" in s for d in dropped_ids)
            )
            touched_rows.update(cdf.index[mask])
            cdf["persons_by_role"] = cdf["persons_by_role"].apply(_relink_brackets)

        n_charters = len(touched_rows)
        cdf.to_csv(charters_path, index=False)

    return {
        "survivor_id": survivor_id,
        "dropped_ids": dropped_ids,
        "resolved_refs_updated": n_resolved,
        "charter_rows_updated": n_charters,
    }
