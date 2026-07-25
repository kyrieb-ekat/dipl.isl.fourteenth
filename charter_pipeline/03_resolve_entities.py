"""
Step 3: Resolve extracted entity strings against the canonical persons/places
tables in charter_pipeline.db, minting provisional rows directly for anything
that isn't a confident match.

Usage:
    python 03_resolve_entities.py --vol 1

Reads:
    output/entities/vol{N}_raw_entities.json
    charter_pipeline.db — persons/places WHERE status='canonical'

Writes:
    output/entities/vol{N}_resolved_entities.json
        — each charter gets resolved_persons[]/resolved_locations[] (with
          person_pk/place_pk ints, or "REVIEW:{pk}" strings for ambiguous
          matches) and new_persons[]/new_places[] (pks minted for this run,
          audit-only -- the rows already exist in the DB by the time this
          file is written, unlike the old CSV pipeline which deferred
          minting to 05_export_csvs.py).
    output/review/vol{N}_review_queue.csv
        — ambiguous matches (score 60-84) for manual inspection, unchanged
          shape from before the SQLite migration.

Note: persons/places are minted straight into the DB as this script runs
(via db.insert_provisional_person/place), not deferred to 05 -- see the
module docstring in 05_export_csvs.py for the other half of this split.
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).parent))
from config import ENTITIES_DIR, REVIEW_DIR, FUZZY_ACCEPT, FUZZY_REVIEW, NEW_PLACE_DEDUP_THRESHOLD
import db
from db import _PAREN_TAIL
from place_authority import PlaceAuthority


def build_lookup(df: pd.DataFrame, id_col: str) -> dict[str, int]:
    """Return {name_form: pk} covering canonical + all variant spellings.
    Same shape as the pre-migration build_lookup(), just keyed to the
    integer person_pk/place_pk instead of the old string id."""
    lookup: dict[str, int] = {}
    for _, row in df.iterrows():
        lookup[row["canonical_name"].strip().lower()] = row[id_col]
        for v in (row["variant_names"] or "").split(";"):
            v = v.strip().lower()
            if v:
                lookup[v] = row[id_col]
    return lookup


def fuzzy_match(name: str, lookup: dict[str, int], authority_df: pd.DataFrame, id_col: str):
    """
    Return (pk_or_None, score, matched_canonical).
    Uses the RapidFuzz token_sort_ratio for tolerance of word-order variants.
    Verbatim algorithm/thresholds from the pre-migration version -- only the
    id type (int pk instead of string id) changed.
    """
    candidates = list(lookup.keys())
    if not candidates:
        return None, 0, ""

    matched_key, score, _ = process.extractOne(
        name.lower(), candidates, scorer=fuzz.token_sort_ratio
    )
    matched_pk = lookup[matched_key]
    row = authority_df[authority_df[id_col] == matched_pk].iloc[0]
    return matched_pk, score, row["canonical_name"]


def _current_max_legacy_num(conn, table: str, volume: int, prefix: str) -> int:
    """Current max numeric suffix among source_volume=volume legacy_ids in
    `table` starting with `prefix` -- the DB-sourced seed for this run's
    legacy_id numbering (replaces the old next_id()'s in-memory-list scan)."""
    rows = conn.execute(
        f"SELECT legacy_id FROM {table} WHERE source_volume = ? AND legacy_id LIKE ?",
        (volume, prefix + "%"),
    ).fetchall()
    best = 0
    for r in rows:
        suffix = r["legacy_id"][len(prefix):]
        if suffix.isdigit():
            best = max(best, int(suffix))
    return best


class LegacyIdSeeder:
    """Seeds legacy_id numbering once per main() invocation from the current
    DB max (see _current_max_legacy_num), then increments purely in-memory
    for the rest of the run -- mirrors the old next_id()'s numbering
    convention (zero-padded to 3 digits) without re-querying the DB per
    insert."""

    def __init__(self, conn, table: str, volume: int, prefix: str):
        self.prefix = prefix
        self.n = _current_max_legacy_num(conn, table, volume, prefix)

    def next_legacy_id(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n:03d}"


def resolve_persons(
    persons: list[dict],
    persons_df: pd.DataFrame,
    persons_lookup: dict,
    volume: int,
    person_seeder: LegacyIdSeeder,
    source_ref: str,
    fuzzy_accept: int = FUZZY_ACCEPT,
    fuzzy_review: int = FUZZY_REVIEW,
    charter_year_str: str = "",
) -> tuple[list[dict], list[int], list[dict]]:
    """
    Returns:
        resolved     — [{name, role_category, qualifier, person_id, match_score}]
                       person_id is an int person_pk, or "REVIEW:{pk}"
        new_person_pks — pks minted for this charter (already live in the DB)
        review_items — ambiguous matches (FUZZY_REVIEW <= score < FUZZY_ACCEPT)
    """
    resolved, new_person_pks, review_items = [], [], []
    seen_new: dict[str, int] = {}  # name -> pk (avoid duplicating within one charter)

    floruit = db.to_int_or_none(charter_year_str)

    for p in persons:
        # .get(field, default) only substitutes when the key is *missing* --
        # the model sometimes emits an explicit "name": null for a
        # role-only mention (e.g. saint-patron qualifiers naming the saint
        # rather than a person), so None must be coalesced before .strip().
        name = (p.get("name") or "").strip()
        if not name:
            continue

        matched_pk, score, canonical = fuzzy_match(name, persons_lookup, persons_df, "person_pk")

        if score >= fuzzy_accept:
            resolved.append({**p, "person_id": matched_pk, "match_score": score, "matched_canonical": canonical})
        elif score >= fuzzy_review:
            review_items.append({
                "type": "person", "extracted_name": name,
                "closest_match": canonical, "match_id": matched_pk, "score": score,
                "role_category": p.get("role_category", ""),
            })
            resolved.append({**p, "person_id": f"REVIEW:{matched_pk}", "match_score": score})
        else:
            key = name.lower()
            if key in seen_new:
                pk = seen_new[key]
            else:
                legacy_id = person_seeder.next_legacy_id()
                pk = db.insert_provisional_person(
                    volume, legacy_id, name,
                    occupation=p.get("role_category") or "",
                    title=p.get("qualifier") or "",
                    # Single-point anchor from the charter's own date, not a
                    # true attested lifespan -- cross-charter matching tools
                    # apply their own +/- tolerance on top of this.
                    floruit_start=floruit, floruit_end=floruit,
                    sources=source_ref,
                )
                seen_new[key] = pk
                new_person_pks.append(pk)
            resolved.append({**p, "person_id": pk, "match_score": 0, "matched_canonical": ""})

    return resolved, new_person_pks, review_items


def _new_place_name_forms(entry: dict) -> list[str]:
    """canonical_name + any variant_names recorded so far for a not-yet-authority
    new place, each with a trailing parenthetical stripped and lowercased —
    mirrors PlaceAuthority.all_names()."""
    forms = [entry["canonical_name"]] + [
        v for v in (entry.get("variant_names") or "").split(";") if v.strip()
    ]
    out = []
    for s in forms:
        s = _PAREN_TAIL.sub("", s).strip().lower()
        if s:
            out.append(s)
    return out


def _match_existing_new_place(name: str, new_places: list[dict], threshold: int) -> tuple[int | None, int]:
    """
    Fuzzy-check `name` against every place already minted as NEW earlier in
    this same charter (new_places is charter-scoped inside resolve_places()).
    Returns (place_pk, score) of the best match at/above `threshold`, else (None, 0).
    """
    best_pk, best_score = None, 0
    key = _PAREN_TAIL.sub("", name).strip().lower() or name.strip().lower()
    for entry in new_places:
        for form in _new_place_name_forms(entry):
            score = fuzz.token_sort_ratio(key, form)
            if score > best_score:
                best_score, best_pk = score, entry["place_pk"]
    return (best_pk, best_score) if best_score >= threshold else (None, 0)


def _record_variant(new_places: list[dict], place_pk: int, spelling: str) -> None:
    """Append a newly-discovered alternate spelling to the matched new place's
    variant_names, both in the in-memory accumulator (so later mentions in
    this same charter have more name forms to match against) and onto the
    already-inserted DB row via db.update_place() (the row is live in the DB
    the moment it's minted, unlike the old CSV pipeline where it only existed
    as an in-memory dict until 05 wrote it out)."""
    for entry in new_places:
        if entry["place_pk"] == place_pk:
            existing = [v for v in (entry.get("variant_names") or "").split(";") if v.strip()]
            if spelling not in existing:
                existing.append(spelling)
            entry["variant_names"] = ";".join(existing)
            db.update_place(place_pk, variant_names=entry["variant_names"])
            return


def resolve_places(
    locations: list[dict],
    all_places: list[str],
    places_df: pd.DataFrame,
    places_lookup: dict,
    volume: int,
    place_seeder: LegacyIdSeeder,
    source_ref: str,
    place_auth: PlaceAuthority | None = None,
    fuzzy_accept: int = FUZZY_ACCEPT,
    fuzzy_review: int = FUZZY_REVIEW,
    new_place_dedup: int = NEW_PLACE_DEDUP_THRESHOLD,
) -> tuple[list[dict], list[int], list[dict]]:
    resolved, review_items = [], []
    seen_new: dict[str, int] = {}
    # Charter-scoped accumulator of places minted as NEW so far in this
    # charter -- only compared against each other (NEW_PLACE_DEDUP_THRESHOLD),
    # never against the whole DB. Holds {"place_pk", "canonical_name",
    # "variant_names"} now that rows are already live in the DB.
    new_place_entries: list[dict] = []

    def resolve_one(name: str, role: str, region: str):
        # Pass 0: check places WHERE status='canonical' (exact match, highest confidence)
        if place_auth:
            entry = place_auth.lookup(name)
            if entry:
                return {"name": name, "role": role, "region": region,
                        "place_id": entry.place_pk, "match_score": 100,
                        "matched_canonical": entry.canonical_name}, None

        matched_pk, score, canonical = fuzzy_match(name, places_lookup, places_df, "place_pk")
        if score >= fuzzy_accept:
            return {"name": name, "role": role, "region": region,
                    "place_id": matched_pk, "match_score": score, "matched_canonical": canonical}, None
        elif score >= fuzzy_review:
            review = {"type": "place", "extracted_name": name,
                      "closest_match": canonical, "match_id": matched_pk, "score": score, "role": role}
            return {"name": name, "role": role, "region": region,
                    "place_id": f"REVIEW:{matched_pk}", "match_score": score}, review
        else:
            key = name.lower()
            if key in seen_new:
                pk = seen_new[key]
            else:
                dup_pk, _dup_score = _match_existing_new_place(name, new_place_entries, new_place_dedup)
                if dup_pk is not None:
                    pk = dup_pk
                    seen_new[key] = pk
                    _record_variant(new_place_entries, pk, name)
                else:
                    legacy_id = place_seeder.next_legacy_id()
                    pk = db.insert_provisional_place(
                        volume, legacy_id, name,
                        region=region or "", sources=source_ref,
                    )
                    seen_new[key] = pk
                    new_place_entries.append({
                        "place_pk": pk, "canonical_name": name, "variant_names": "",
                    })
            return {"name": name, "role": role, "region": region,
                    "place_id": pk, "match_score": 0}, None

    for loc in locations:
        r, review = resolve_one((loc.get("name") or ""), loc.get("role", "loc.mentioned"), loc.get("region", ""))
        resolved.append(r)
        if review:
            review_items.append(review)

    # Also resolve bare place names from all_places_mentioned (loc.mentioned)
    already_named = {(loc.get("name") or "").lower() for loc in locations}
    for name in all_places:
        if name.lower() not in already_named:
            r, review = resolve_one(name, "loc.mentioned", "")
            resolved.append(r)
            if review:
                review_items.append(review)
            already_named.add(name.lower())

    new_place_pks = [e["place_pk"] for e in new_place_entries]
    return resolved, new_place_pks, review_items


def main():
    parser = argparse.ArgumentParser(description="Resolve entities against the persons/places tables.")
    parser.add_argument("--vol", type=int, required=True)
    parser.add_argument("--fuzzy-accept", type=int, default=FUZZY_ACCEPT,
                        help=f"Min score to auto-assign an existing pk (default: {FUZZY_ACCEPT})")
    parser.add_argument("--fuzzy-review", type=int, default=FUZZY_REVIEW,
                        help=f"Min score to flag for manual review (default: {FUZZY_REVIEW})")
    args = parser.parse_args()
    fa, fr = args.fuzzy_accept, args.fuzzy_review
    print(f"Thresholds: auto-assign >= {fa}, review >= {fr}, new < {fr}")

    raw_path = ENTITIES_DIR / f"vol{args.vol:02d}_raw_entities.json"
    if not raw_path.exists():
        print(f"Error: {raw_path} not found. Run 02_extract_entities.py first.", file=sys.stderr)
        sys.exit(1)

    with open(raw_path, encoding="utf-8") as f:
        charters = json.load(f)

    persons_df = db.get_persons(status="canonical")
    places_df = db.get_places(status="canonical")
    persons_lookup = build_lookup(persons_df, "person_pk")
    places_lookup = build_lookup(places_df, "place_pk")

    place_auth = PlaceAuthority()  # in-memory index over places WHERE status='canonical'

    conn = db.get_connection()
    try:
        person_seeder = LegacyIdSeeder(conn, "persons", args.vol, "p")
        place_seeder = LegacyIdSeeder(conn, "places", args.vol, "l")
    finally:
        conn.close()

    resolved_charters = []
    all_new_person_pks: list[int] = []
    all_new_place_pks: list[int] = []
    all_review_items: list[dict] = []

    for ch in charters:
        if "_parse_error" in ch or "_api_error" in ch:
            resolved_charters.append({**ch, "_skipped": True})
            continue

        source_ref = f"DI vol.{args.vol} seq.{ch.get('sequence','?')} | {ch.get('di_reference','')}"

        res_persons, new_p_pks, rev_p = resolve_persons(
            ch.get("persons", []), persons_df, persons_lookup, args.vol, person_seeder, source_ref,
            fuzzy_accept=fa, fuzzy_review=fr, charter_year_str=db.charter_year(ch.get("date")),
        )
        res_places, new_l_pks, rev_l = resolve_places(
            ch.get("locations", []), ch.get("all_places_mentioned", []),
            places_df, places_lookup, args.vol, place_seeder, source_ref, place_auth,
            fuzzy_accept=fa, fuzzy_review=fr,
        )

        all_new_person_pks.extend(new_p_pks)
        all_new_place_pks.extend(new_l_pks)
        all_review_items.extend([{**r, "charter_filename": ch["filename"], "charter_date": ch.get("date", "")}
                                  for r in rev_p + rev_l])

        resolved_charters.append({
            **ch,
            "resolved_persons": res_persons,
            "resolved_locations": res_places,
            "new_persons": new_p_pks,
            "new_places": new_l_pks,
        })

    # Save resolved JSON
    out_path = ENTITIES_DIR / f"vol{args.vol:02d}_resolved_entities.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(resolved_charters, f, ensure_ascii=False, indent=2)

    # Save review queue CSV -- unchanged shape from before the SQLite migration.
    # DB wiring of review_queue_items (direct FKs to charter_persons/charter_places)
    # is deferred to a later phase; this CSV remains the only review-queue
    # output this script produces.
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    review_path = REVIEW_DIR / f"vol{args.vol:02d}_review_queue.csv"
    if all_review_items:
        import csv
        review_fields = ["type", "extracted_name", "closest_match", "match_id", "score",
                         "role_category", "role", "charter_filename", "charter_date"]
        with open(review_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=review_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_review_items)

    print(f"Resolved {len(resolved_charters)} charters.")
    print(f"  {len(all_new_person_pks)} new persons  |  {len(all_new_place_pks)} new places")
    print(f"  {len(all_review_items)} items flagged for manual review → {review_path}")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
